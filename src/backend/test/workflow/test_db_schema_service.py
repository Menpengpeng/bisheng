"""F043 T005 — schema fetch, intersection and Redis cache behaviour."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bisheng.common.errcode.flow import (
    DbConnectionFailedError,
    DbDriverMissingError,
    DbInspectTimeoutError,
    DbSchemaNoValidTableError,
)
from bisheng.workflow.nodes.agent import db_schema_service as svc
from bisheng.workflow.nodes.agent.db_schema_service import (
    DEFAULT_CACHE_TTL_HOURS,
    MAX_CACHE_TTL_HOURS,
    MIN_CACHE_TTL_HOURS,
    SCHEMA_CACHE_TTL,
    build_cache_key,
    build_sql_uri,
    get_schema_ddl,
    get_schema_with_cache,
    list_tables,
    refresh_schema_cache,
    resolve_cache_ttl_seconds,
)


def _params(engine='mysql', tables=None, cache_on=False, ttl=None):
    ns = SimpleNamespace(
        database_engine=engine,
        db_address='h:3306',
        db_name='demo',
        db_username='u',
        db_password='p',
        selected_tables=tables or [],
        schema_cache_enabled=cache_on,
    )
    if ttl is not None:
        ns.schema_cache_ttl = ttl
    return ns


class FakeEngine:
    def __init__(self):
        self.disposed = 0

    def dispose(self):
        self.disposed += 1


class FakeSQLDatabase:
    def __init__(self, tables, ddl='CREATE TABLE x (id INT)'):
        self._tables = set(tables)
        self._engine = FakeEngine()
        self.ddl = ddl
        self.info_calls = []

    def get_usable_table_names(self):
        return sorted(self._tables)

    def get_table_info(self, table_names, get_col_comments=False):
        self.info_calls.append((list(table_names), get_col_comments))
        return self.ddl


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.last_ttl = None
        self.get_raises = False
        self.set_raises = False
        self.delete_raises = False

    def get(self, key):
        if self.get_raises:
            raise ConnectionError('redis down')
        return self.store.get(key)

    def set(self, key, value, expiration=3600):
        if self.set_raises:
            raise ConnectionError('redis down')
        self.store[key] = value
        self.last_ttl = expiration
        return True

    def delete(self, key):
        if self.delete_raises:
            raise ConnectionError('redis down')
        self.store.pop(key, None)
        return 1


@pytest.fixture()
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(svc, 'get_redis_client_sync', lambda: client)
    return client


@pytest.fixture()
def captured_from_uri(monkeypatch):
    captured = {}

    def _factory(uri, engine_args=None, **kwargs):
        captured['uri'] = uri
        captured['engine_args'] = engine_args
        captured.update(kwargs)
        return FakeSQLDatabase(['users', 'orders', 'products'])

    monkeypatch.setattr('langchain_community.utilities.SQLDatabase.from_uri',
                        staticmethod(_factory))
    return captured


# ---------------------------------------------------------------------------
# cache key
# ---------------------------------------------------------------------------

def test_cache_key_order_insensitive():
    meta = build_sql_uri(_params())
    k1 = build_cache_key(1, meta, ['a', 'b'])
    k2 = build_cache_key(1, meta, ['b', 'a'])
    assert k1 == k2


def test_cache_key_varies_by_tenant_db_user_tables():
    base = build_sql_uri(_params())
    assert build_cache_key(1, base, ['a']) != build_cache_key(2, base, ['a'])
    assert build_cache_key(1, base, ['a']) != build_cache_key(1, base, ['b'])

    params_db = SimpleNamespace(database_engine='mysql', db_address='h:3306',
                                db_name='other', db_username='u', db_password='p')
    assert build_cache_key(1, base, ['a']) != build_cache_key(
        1, build_sql_uri(params_db), ['a'])

    params_user = SimpleNamespace(database_engine='mysql', db_address='h:3306',
                                  db_name='demo', db_username='u2', db_password='p')
    assert build_cache_key(1, base, ['a']) != build_cache_key(
        1, build_sql_uri(params_user), ['a'])


def test_cache_key_does_not_contain_password():
    meta = build_sql_uri(_params())
    key = build_cache_key(1, meta, ['users'])
    assert 'p' != key.split(':')[-1]
    assert 's3cret' not in key


# ---------------------------------------------------------------------------
# schema fetch / intersection
# ---------------------------------------------------------------------------

def test_get_schema_ddl_intersection_and_zero_sample_rows(captured_from_uri):
    result = get_schema_ddl(_params(), ['users', 'ghost'])
    assert result.found == ['users']
    assert result.missing == ['ghost']
    assert result.from_cache is False
    assert result.ddl
    assert result.fetched_at
    assert captured_from_uri['sample_rows_in_table_info'] == 0


def test_get_schema_ddl_all_missing_raises(captured_from_uri):
    with pytest.raises(DbSchemaNoValidTableError):
        get_schema_ddl(_params(), ['nope1', 'nope2'])


def test_get_schema_ddl_disposes_connection(captured_from_uri):
    # FakeSQLDatabase is created inside the factory; capture the instance.
    instances = []
    import langchain_community.utilities as u

    original = u.SQLDatabase.from_uri

    def _factory(uri, engine_args=None, **kwargs):
        db = FakeSQLDatabase(['users'])
        instances.append(db)
        return db

    u.SQLDatabase.from_uri = staticmethod(_factory)
    try:
        get_schema_ddl(_params(), ['users'])
    finally:
        u.SQLDatabase.from_uri = original
    assert instances and instances[0]._engine.disposed == 1


def test_list_tables_and_truncated_flag(monkeypatch):
    import langchain_community.utilities as u

    db = FakeSQLDatabase([f't{i}' for i in range(svc.TABLE_LIST_SOFT_LIMIT + 1)])
    monkeypatch.setattr(u.SQLDatabase, 'from_uri',
                        staticmethod(lambda uri, engine_args=None, **kw: db))
    tables, truncated = list_tables(_params())
    assert len(tables) == svc.TABLE_LIST_SOFT_LIMIT + 1
    assert truncated is True
    assert db._engine.disposed == 1


# ---------------------------------------------------------------------------
# cache behaviour
# ---------------------------------------------------------------------------

def test_cache_miss_fetches_and_writes_with_ttl(fake_redis, captured_from_uri):
    params = _params(tables=['users'], cache_on=True)
    result = get_schema_with_cache(params, tenant_id=7)
    assert result.from_cache is False
    assert result.found == ['users']
    assert fake_redis.last_ttl == SCHEMA_CACHE_TTL
    assert any(v['ddl'] == result.ddl for v in fake_redis.store.values())


@pytest.mark.parametrize(
    'given,expected_hours',
    [
        (None, DEFAULT_CACHE_TTL_HOURS),     # missing -> 24h default
        ('', DEFAULT_CACHE_TTL_HOURS),       # empty -> default
        ('abc', DEFAULT_CACHE_TTL_HOURS),    # non-numeric -> default
        (6, 6),                              # normal value passthrough
        (0, MIN_CACHE_TTL_HOURS),            # below range -> clamped up
        (-5, MIN_CACHE_TTL_HOURS),
        (99999, MAX_CACHE_TTL_HOURS),        # above range -> clamped down
    ],
)
def test_resolve_cache_ttl_seconds(given, expected_hours):
    assert resolve_cache_ttl_seconds(given) == expected_hours * 3600


def test_custom_ttl_is_applied_on_cache_miss(fake_redis, captured_from_uri):
    params = _params(tables=['users'], cache_on=True, ttl=6)
    get_schema_with_cache(params, tenant_id=7)
    assert fake_redis.last_ttl == 6 * 3600


def test_custom_ttl_is_applied_on_refresh(fake_redis, captured_from_uri):
    params = _params(tables=['users'], cache_on=True, ttl=2)
    refresh_schema_cache(params, tenant_id=7)
    assert fake_redis.last_ttl == 2 * 3600
    assert list(fake_redis.store.keys())  # entry was rebuilt


def test_cache_hit_skips_source(fake_redis, monkeypatch):
    params = _params(tables=['users'], cache_on=True)
    meta = build_sql_uri(params)
    key = build_cache_key(7, meta, ['users'])
    fake_redis.store[key] = {'dialect': 'mysql', 'tables': ['users'],
                             'ddl': 'CACHED DDL', 'fetched_at': '2026-01-01T00:00:00+00:00'}

    def _boom(*a, **k):
        raise AssertionError('must not connect when cache hits')

    monkeypatch.setattr('langchain_community.utilities.SQLDatabase.from_uri',
                        staticmethod(_boom))
    result = get_schema_with_cache(params, tenant_id=7)
    assert result.from_cache is True
    assert result.ddl == 'CACHED DDL'


def test_cache_disabled_never_reads_or_writes(fake_redis, captured_from_uri):
    params = _params(tables=['users'], cache_on=False)
    result = get_schema_with_cache(params, tenant_id=7)
    assert result.from_cache is False
    assert fake_redis.store == {}


def test_cache_read_failure_degrades_to_source(fake_redis, captured_from_uri):
    fake_redis.get_raises = True
    result = get_schema_with_cache(_params(tables=['users'], cache_on=True), 7)
    assert result.from_cache is False
    assert result.found == ['users']


def test_cache_write_failure_does_not_raise(fake_redis, captured_from_uri):
    fake_redis.set_raises = True
    result = get_schema_with_cache(_params(tables=['users'], cache_on=True), 7)
    assert result.found == ['users']


def test_refresh_deletes_then_rebuilds(fake_redis, captured_from_uri):
    params = _params(tables=['users'], cache_on=True)
    meta = build_sql_uri(params)
    key = build_cache_key(7, meta, ['users'])
    fake_redis.store[key] = {'ddl': 'STALE'}
    result = refresh_schema_cache(params, tenant_id=7)
    assert result.from_cache is False
    assert fake_redis.store[key]['ddl'] == result.ddl
    assert fake_redis.store[key]['ddl'] != 'STALE'


# ---------------------------------------------------------------------------
# connection error normalization
# ---------------------------------------------------------------------------

def test_driver_missing_normalized(monkeypatch):
    def _boom(uri, engine_args=None, **kw):
        raise ModuleNotFoundError("No module named 'pyodbc'")

    monkeypatch.setattr('langchain_community.utilities.SQLDatabase.from_uri',
                        staticmethod(_boom))
    with pytest.raises(DbDriverMissingError):
        list_tables(_params())


def test_timeout_normalized(monkeypatch):
    def _boom(uri, engine_args=None, **kw):
        raise OSError('connection timed out')

    monkeypatch.setattr('langchain_community.utilities.SQLDatabase.from_uri',
                        staticmethod(_boom))
    with pytest.raises(DbInspectTimeoutError):
        list_tables(_params())


def test_generic_connection_error_normalized(monkeypatch):
    def _boom(uri, engine_args=None, **kw):
        raise RuntimeError('Access denied for user')

    monkeypatch.setattr('langchain_community.utilities.SQLDatabase.from_uri',
                        staticmethod(_boom))
    with pytest.raises(DbConnectionFailedError):
        list_tables(_params())
