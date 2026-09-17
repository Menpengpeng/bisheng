"""F043 T003 — unit tests for ``build_sql_uri`` dialect normalization."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from bisheng.common.errcode.flow import DbConnectionFailedError
from bisheng.workflow.nodes.agent.db_schema_service import build_sql_uri


def _params(engine: str, address: str = 'db.example.com', db_name: str = 'demo',
            username: str = 'u', password: str = 'p'):
    return SimpleNamespace(
        database_engine=engine,
        db_address=address,
        db_name=db_name,
        db_username=username,
        db_password=password,
    )


@pytest.mark.parametrize('engine,driver,port', [
    ('mysql', 'mysql+pymysql', 3306),
    ('db2', 'db2+ibm_db', 50000),
    ('postgres', 'postgresql+psycopg2', 5432),
    ('postgresql', 'postgresql+psycopg2', 5432),
    ('gaussdb', 'opengauss+psycopg2', 5432),
    ('oracle', 'oracle+oracledb', 1521),
    ('sqlserver', 'mssql+pyodbc', 1433),
    ('dm', 'dm+dmPython', 5236),
])
def test_default_port_and_driver(engine, driver, port):
    meta = build_sql_uri(_params(engine, address='h'))
    url = make_url(meta.uri)
    assert url.drivername == driver
    assert url.host == 'h'
    assert url.port == port
    assert meta.host == 'h'
    assert meta.port == port
    assert meta.username == 'u'
    assert meta.db_name == 'demo'


def test_explicit_port_overrides_default():
    meta = build_sql_uri(_params('mysql', address='h:3307'))
    assert make_url(meta.uri).port == 3307
    assert meta.port == 3307


def test_mysql_charset_query():
    url = make_url(build_sql_uri(_params('mysql')).uri)
    assert url.query.get('charset') == 'utf8mb4'
    assert url.database == 'demo'


def test_oracle_uses_service_name_not_database():
    url = make_url(build_sql_uri(_params('oracle')).uri)
    assert url.database is None
    assert url.query.get('service_name') == 'demo'


def test_sqlserver_odbc_query():
    url = make_url(build_sql_uri(_params('sqlserver')).uri)
    assert url.database == 'demo'
    assert url.query.get('driver') == 'ODBC Driver 18 for SQL Server'
    assert url.query.get('TrustServerCertificate') == 'yes'


def test_dm_uses_schema_query_without_database_position():
    url = make_url(build_sql_uri(_params('dm')).uri)
    assert url.drivername == 'dm+dmPython'
    assert url.database is None
    assert url.query.get('schema') == 'demo'


def test_dialect_normalized_canonical():
    assert build_sql_uri(_params('postgres')).dialect == 'postgresql'
    assert build_sql_uri(_params('SQLServer')).dialect == 'mssql'
    assert build_sql_uri(_params('DM')).dialect == 'dm'
    assert build_sql_uri(_params('DM8')).dialect == 'dm'
    assert build_sql_uri(_params('MySQL')).dialect == 'mysql'


def test_uri_carries_password_but_fingerprint_does_not():
    meta = build_sql_uri(_params('mysql', password='s3cret'))
    assert 's3cret' in meta.uri
    # URIResult exposes no password attribute for cache/log reuse
    assert not hasattr(meta, 'password')


def test_unsupported_engine_raises_business_error():
    with pytest.raises(DbConnectionFailedError):
        build_sql_uri(_params('sqlite'))
