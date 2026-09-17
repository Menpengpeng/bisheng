"""F043 — config-time database inspection endpoint tests.

Mounts only the workflow router on a minimal FastAPI app and overrides the
login dependency. The blocking service functions (``list_tables`` /
``refresh_schema_cache``) are monkeypatched so no real database is touched.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bisheng.api.v1 import workflow as workflow_api
from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.common.errcode.flow import DbConnectionFailedError
from bisheng.workflow.nodes.agent.db_schema_service import SchemaResult

BASE = '/api/v1/workflow'

_CONN_BODY = {
    'database_engine': 'mysql',
    'db_address': '127.0.0.1:3306',
    'db_name': 'demo',
    'db_username': 'root',
    'db_password': 'secret',
}


def _client(tenant_id: int = 7):
    app = FastAPI()
    app.include_router(workflow_api.router, prefix='/api/v1')

    async def _login_user():
        return SimpleNamespace(user_id=1, user_name='u', tenant_id=tenant_id)

    app.dependency_overrides[UserPayload.get_login_user] = _login_user
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /workflow/db/tables
# ---------------------------------------------------------------------------

def test_list_tables_success(monkeypatch):
    monkeypatch.setattr(workflow_api, 'list_tables',
                        lambda payload: (['users', 'orders'], False))
    resp = _client().post(f'{BASE}/db/tables', json=_CONN_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body['status_code'] == 200
    assert body['data'] == {'tables': ['users', 'orders'], 'truncated': False}


def test_list_tables_truncated_flag(monkeypatch):
    monkeypatch.setattr(workflow_api, 'list_tables',
                        lambda payload: (['t'], True))
    resp = _client().post(f'{BASE}/db/tables', json=_CONN_BODY)
    assert resp.json()['data']['truncated'] is True


def test_list_tables_passes_connection_params(monkeypatch):
    captured = {}

    def _fake(payload):
        captured.update(payload.model_dump())
        return [], False

    monkeypatch.setattr(workflow_api, 'list_tables', _fake)
    _client().post(f'{BASE}/db/tables', json=_CONN_BODY)
    assert captured['database_engine'] == 'mysql'
    assert captured['db_address'] == '127.0.0.1:3306'
    assert captured['db_password'] == 'secret'


def test_list_tables_business_error_maps_code(monkeypatch):
    def _raise(payload):
        raise DbConnectionFailedError()

    monkeypatch.setattr(workflow_api, 'list_tables', _raise)
    resp = _client().post(f'{BASE}/db/tables', json=_CONN_BODY)
    assert resp.status_code == 200
    assert resp.json()['status_code'] == 10560


def test_list_tables_validation_error_on_missing_field():
    body = {k: v for k, v in _CONN_BODY.items() if k != 'db_password'}
    resp = _client().post(f'{BASE}/db/tables', json=body)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /workflow/db/schema/refresh
# ---------------------------------------------------------------------------

def test_refresh_schema_success(monkeypatch):
    monkeypatch.setattr(
        workflow_api, 'refresh_schema_cache',
        lambda payload, tenant_id: SchemaResult(
            found=['users'], missing=['gone'], ddl='CREATE TABLE users (id INT)',
            fetched_at='2026-09-17T00:00:00Z', from_cache=False),
    )
    body = {**_CONN_BODY, 'selected_tables': ['users', 'gone'],
            'schema_cache_enabled': True}
    resp = _client(tenant_id=9).post(f'{BASE}/db/schema/refresh', json=body)
    assert resp.status_code == 200
    data = resp.json()['data']
    assert data['tables'] == ['users']
    assert data['missing_tables'] == ['gone']
    assert data['fetched_at'] == '2026-09-17T00:00:00Z'
    assert data['from_cache'] is False


def test_refresh_schema_passes_tenant_and_tables(monkeypatch):
    captured = {}

    def _fake(payload, tenant_id):
        captured['tenant'] = tenant_id
        captured['tables'] = list(payload.selected_tables)
        captured['cache'] = payload.schema_cache_enabled
        return SchemaResult(found=payload.selected_tables)

    monkeypatch.setattr(workflow_api, 'refresh_schema_cache', _fake)
    body = {**_CONN_BODY, 'selected_tables': ['a', 'b'],
            'schema_cache_enabled': True}
    _client(tenant_id=12).post(f'{BASE}/db/schema/refresh', json=body)
    assert captured['tenant'] == 12
    assert captured['tables'] == ['a', 'b']
    assert captured['cache'] is True


def test_refresh_schema_business_error_maps_code(monkeypatch):
    def _raise(payload, tenant_id):
        raise DbConnectionFailedError()

    monkeypatch.setattr(workflow_api, 'refresh_schema_cache', _raise)
    body = {**_CONN_BODY, 'selected_tables': ['nope']}
    resp = _client().post(f'{BASE}/db/schema/refresh', json=body)
    assert resp.json()['status_code'] == 10560


def test_refresh_schema_defaults_optional_fields(monkeypatch):
    captured = {}

    def _fake(payload, tenant_id):
        captured['tables'] = payload.selected_tables
        captured['cache'] = payload.schema_cache_enabled
        return SchemaResult()

    monkeypatch.setattr(workflow_api, 'refresh_schema_cache', _fake)
    resp = _client().post(f'{BASE}/db/schema/refresh', json=_CONN_BODY)
    assert resp.status_code == 200
    assert captured['tables'] == []
    assert captured['cache'] is False


# ---------------------------------------------------------------------------
# Static: login dependency is enforced on both routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['/workflow/db/tables', '/workflow/db/schema/refresh'])
def test_routes_require_login(path):
    route = next(r for r in workflow_api.router.routes if r.path == path)

    def _has_login_dep(dependant):
        for dep in dependant.dependencies:
            if getattr(dep.call, '__name__', '') == 'get_login_user':
                return True
            if _has_login_dep(dep):
                return True
        return False

    assert _has_login_dep(route.dependant)
