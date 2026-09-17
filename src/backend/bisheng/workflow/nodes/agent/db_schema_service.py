"""F043: SQL schema inspection and cache service for the workflow assistant node.

This service lives in the main ``bisheng`` package (not ``bisheng_langchain``)
because it needs tenant context and the main Redis client. It is the single
place that:

1. Builds SQLAlchemy URIs for the supported database engines (including DM8).
2. Lists tables / fetches DDL schema text via langchain ``SQLDatabase``.
3. Reads/writes the Redis schema cache (TTL based, failure tolerant).

The langchain sql_agent tool only receives the prepared ``schema_ddl`` text and
``selected_tables``; it never imports this module.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from loguru import logger
from sqlalchemy.engine import URL

from bisheng.common.errcode.flow import (
    DbConnectionFailedError,
    DbDriverMissingError,
    DbInspectTimeoutError,
    DbSchemaNoValidTableError,
)
from bisheng.core.cache.redis_manager import get_redis_client_sync

# Cache configuration
SCHEMA_CACHE_PREFIX = 'wf:sqlschema:'
DEFAULT_CACHE_TTL_HOURS = 24  # user-facing default
SCHEMA_CACHE_TTL = DEFAULT_CACHE_TTL_HOURS * 3600  # default TTL in seconds
# The user-configured TTL is expressed in hours; clamp it to a sane range.
MIN_CACHE_TTL_HOURS = 1
MAX_CACHE_TTL_HOURS = 720  # 30 days
TABLE_LIST_SOFT_LIMIT = 2000
CONNECT_TIMEOUT_SECONDS = 10

# Engines whose connect_args accept a connect_timeout value
_TIMEOUT_CONNECT_ARGS_ENGINES = {'mysql', 'postgresql'}

# Engines whose inspector supports column comments in get_table_info
_COL_COMMENT_ENGINES = {'mysql', 'postgresql', 'oracle'}


class SqlAgentConfig(Protocol):
    """Structural type satisfied by ``SqlAgentParams`` (avoids circular import)."""

    database_engine: str | None
    db_username: str
    db_password: str
    db_address: str
    db_name: str


@dataclass
class URIResult:
    """Normalized connection descriptor; never carries the password field."""

    dialect: str
    uri: str
    host: str
    port: int
    db_name: str
    username: str


@dataclass
class SchemaResult:
    """DDL fetch outcome."""

    found: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    ddl: str = ''
    fetched_at: str = ''
    from_cache: bool = False


# driver name, default port per normalized dialect
_DIALECT_SPEC: dict[str, tuple[str, int]] = {
    'mysql': ('mysql+pymysql', 3306),
    'postgresql': ('postgresql+psycopg2', 5432),
    'oracle': ('oracle+oracledb', 1521),
    'mssql': ('mssql+pyodbc', 1433),
    'db2': ('db2+ibm_db', 50000),
    'gaussdb': ('opengauss+psycopg2', 5432),
    'dm': ('dm+dmPython', 5236),
}


def _normalize_dialect(engine: str | None) -> str:
    """Map the config engine value (case-insensitive) to a canonical dialect."""
    engine = (engine or '').lower()
    if engine in ('postgres', 'postgresql'):
        return 'postgresql'
    if engine == 'sqlserver':
        return 'mssql'
    if engine == 'dm8':
        return 'dm'
    if engine in _DIALECT_SPEC:
        return engine
    raise DbConnectionFailedError(msg=f'Unsupported database engine: {engine}')


def _parse_host_port(address: str, default_port: int) -> tuple[str, int]:
    """Parse ``host`` / ``host:port``; fall back to the dialect default port."""
    address = (address or '').strip()
    if ':' in address:
        host, port = address.rsplit(':', 1)
        return host, int(port)
    return address, default_port


def build_sql_uri(params: SqlAgentConfig) -> URIResult:
    """Build the SQLAlchemy URI and normalized connection fingerprint.

    DM8 (dmPython) requires the database name as the ``schema`` query parameter
    instead of the URL database path.
    """
    dialect = _normalize_dialect(params.database_engine)
    driver, default_port = _DIALECT_SPEC[dialect]
    host, port = _parse_host_port(params.db_address, default_port)

    query: dict[str, str] = {}
    database = params.db_name
    if dialect == 'mysql':
        query = {'charset': 'utf8mb4'}
    elif dialect == 'oracle':
        # oracledb uses service_name instead of a database path
        query = {'service_name': params.db_name}
        database = None
    elif dialect == 'mssql':
        query = {
            'driver': 'ODBC Driver 18 for SQL Server',
            'TrustServerCertificate': 'yes',
        }
    elif dialect == 'dm':
        # dmPython rejects the database URL position; use ?schema=
        query = {'schema': params.db_name}
        database = None

    url = URL.create(
        driver,
        username=params.db_username,
        password=params.db_password,
        host=host,
        port=port,
        database=database,
        query=query,
    )
    uri = url.render_as_string(hide_password=False)
    return URIResult(
        dialect=dialect,
        uri=uri,
        host=host,
        port=port,
        db_name=params.db_name,
        username=params.db_username,
    )


def resolve_cache_ttl_seconds(ttl_hours) -> int:
    """Resolve the user-configured TTL (hours) to a clamped Redis TTL (seconds).

    Missing/non-numeric values fall back to the 24h default; out-of-range
    values are clamped instead of rejected so a saved config never blocks a run.
    """
    try:
        hours = int(ttl_hours)
    except (TypeError, ValueError):
        return SCHEMA_CACHE_TTL
    hours = max(MIN_CACHE_TTL_HOURS, min(MAX_CACHE_TTL_HOURS, hours))
    return hours * 3600


def build_cache_key(tenant_id: int, meta: URIResult, table_names: list[str]) -> str:
    """Build the redis key; the password is deliberately excluded."""
    tables = ','.join(sorted(table_names))
    raw = '|'.join([
        meta.dialect,
        meta.host,
        str(meta.port),
        meta.db_name,
        meta.username,
        tables,
    ])
    digest = hashlib.md5(raw.encode('utf-8')).hexdigest()
    return f'{SCHEMA_CACHE_PREFIX}{tenant_id}:{digest}'


def _connect(meta: URIResult):
    """Open a short-lived SQLDatabase; normalize connection errors."""
    from langchain_community.utilities import SQLDatabase

    engine_args = {}
    if meta.dialect in _TIMEOUT_CONNECT_ARGS_ENGINES:
        engine_args['connect_args'] = {'connect_timeout': CONNECT_TIMEOUT_SECONDS}
    try:
        # No include_tables here: a missing selected table would raise during
        # construction. Intersection is computed by the callers.
        return SQLDatabase.from_uri(
            meta.uri,
            engine_args=engine_args,
            sample_rows_in_table_info=0,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        logger.warning('sql schema inspect driver missing: {}', exc)
        raise DbDriverMissingError(exception=exc) from exc
    except (TimeoutError, OSError) as exc:
        if _is_timeout(exc):
            logger.warning('sql schema inspect timeout: {}', type(exc).__name__)
            raise DbInspectTimeoutError(exception=exc) from exc
        raise DbConnectionFailedError(exception=exc) from exc
    except Exception as exc:
        if _is_driver_missing(exc):
            logger.warning('sql schema inspect driver missing: {}', exc)
            raise DbDriverMissingError(exception=exc) from exc
        if _is_timeout(exc):
            logger.warning('sql schema inspect timeout: {}', type(exc).__name__)
            raise DbInspectTimeoutError(exception=exc) from exc
        logger.warning('sql schema inspect connection failed: {}', type(exc).__name__)
        raise DbConnectionFailedError(exception=exc) from exc


def _is_driver_missing(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "can't open lib",
        'odbc driver',
        'no module named',
        'no such driver',
        'driver not found',
        'libclntsh',
        'db2cli',
        'dmpython',
    )
    return any(marker in text for marker in markers)


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return 'timeout' in text or 'timed out' in text


def list_tables(params: SqlAgentConfig) -> tuple[list[str], bool]:
    """List usable tables for configuration-time selection.

    Returns ``(tables, truncated)``. The list is not hard truncated; the
    truncated flag only hints the UI to encourage keyword search.
    """
    meta = build_sql_uri(params)
    db = _connect(meta)
    try:
        tables = sorted(db.get_usable_table_names())
    finally:
        db._engine.dispose()
    truncated = len(tables) > TABLE_LIST_SOFT_LIMIT
    logger.info('act=sql_table_list engine={} tables={} truncated={}',
                meta.dialect, len(tables), truncated)
    return tables, truncated


def get_schema_ddl(params: SqlAgentConfig, selected_tables: list[str]) -> SchemaResult:
    """Fetch DDL for the selected tables in real time (no cache involved).

    Tables missing from the database are only warned about; an empty
    intersection raises DbSchemaNoValidTableError.
    """
    meta = build_sql_uri(params)
    db = _connect(meta)
    try:
        actual = set(db.get_usable_table_names())
        selected = set(selected_tables)
        found = sorted(actual & selected)
        missing = sorted(selected - actual)
        if missing:
            logger.warning('act=sql_schema_missing engine={} missing={}',
                           meta.dialect, missing)
        if not found:
            raise DbSchemaNoValidTableError()
        with_comments = meta.dialect in _COL_COMMENT_ENGINES
        ddl = db.get_table_info(found, get_col_comments=with_comments)
    finally:
        db._engine.dispose()

    fetched_at = datetime.now(UTC).isoformat()
    logger.info('act=sql_schema_fetch engine={} tables={} missing={}',
                meta.dialect, len(found), len(missing))
    return SchemaResult(found=found, missing=missing, ddl=ddl, fetched_at=fetched_at)


def _cache_payload(result: SchemaResult, dialect: str) -> dict:
    return {
        'dialect': dialect,
        'tables': result.found,
        'ddl': result.ddl,
        'fetched_at': result.fetched_at,
    }


def get_schema_with_cache(params: SqlAgentConfig, tenant_id: int) -> SchemaResult:
    """Return schema DDL, using the Redis cache when the toggle is enabled.

    Cache read/write failures degrade to fetching from source and never block
    the workflow run.
    """
    selected_tables = getattr(params, 'selected_tables', None) or []
    cache_enabled = getattr(params, 'schema_cache_enabled', False)
    ttl_seconds = resolve_cache_ttl_seconds(getattr(params, 'schema_cache_ttl', None))
    meta = build_sql_uri(params)

    if cache_enabled and selected_tables:
        key = build_cache_key(tenant_id, meta, selected_tables)
        try:
            payload = get_redis_client_sync().get(key)
        except Exception as exc:
            logger.warning('act=sql_schema_cache_read_failed err={}', type(exc).__name__)
            payload = None
        if isinstance(payload, dict) and payload.get('ddl'):
            logger.info('act=sql_schema_cache_hit engine={} tables={}',
                        meta.dialect, len(payload.get('tables', [])))
            return SchemaResult(
                found=list(payload.get('tables', [])),
                missing=[],
                ddl=payload['ddl'],
                fetched_at=payload.get('fetched_at', ''),
                from_cache=True,
            )

    result = get_schema_ddl(params, selected_tables)

    if cache_enabled and selected_tables:
        # Reuse the selection-derived key so a partial-miss fetch still hits
        # the same entry next time.
        try:
            get_redis_client_sync().set(key, _cache_payload(result, meta.dialect),
                                        expiration=ttl_seconds)
        except Exception as exc:
            logger.warning('act=sql_schema_cache_write_failed err={}', type(exc).__name__)

    return result


def refresh_schema_cache(params: SqlAgentConfig, tenant_id: int) -> SchemaResult:
    """Delete the existing cache entry (if any) and rebuild it from source."""
    selected_tables = getattr(params, 'selected_tables', None) or []
    ttl_seconds = resolve_cache_ttl_seconds(getattr(params, 'schema_cache_ttl', None))
    meta = build_sql_uri(params)
    key = build_cache_key(tenant_id, meta, selected_tables)
    try:
        get_redis_client_sync().delete(key)
    except Exception as exc:
        logger.warning('act=sql_schema_cache_delete_failed err={}', type(exc).__name__)

    result = get_schema_ddl(params, selected_tables)

    try:
        get_redis_client_sync().set(key, _cache_payload(result, meta.dialect),
                                   expiration=ttl_seconds)
    except Exception as exc:
        logger.warning('act=sql_schema_cache_write_failed err={}', type(exc).__name__)

    logger.info('act=sql_schema_cache_refresh engine={} tables={} ttl_s={}',
                meta.dialect, len(result.found), ttl_seconds)
    return result
