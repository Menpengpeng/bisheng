"""Tests for ``scripts/backfill_department_parent_tuples.py``.

DB-integration is skipped (sqlite fixtures lack the full schema); these
cover the dry-run/apply orchestration with the edge collector and the
OpenFGA writer mocked.
"""

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

import scripts.backfill_department_parent_tuples as mod


@pytest.fixture(autouse=True)
def _stub_tenant_filter():
    with patch(
        "bisheng.core.context.tenant.bypass_tenant_filter",
        new=lambda: contextlib.nullcontext(),
    ):
        yield


def _patch_edges(edges):
    return patch.object(
        mod,
        "_collect_parent_edges",
        new_callable=AsyncMock,
        return_value=edges,
    )


def _patch_writer(existing: dict[str, tuple] | None = None):
    permissions = AsyncMock()
    permissions.apply_changes = AsyncMock()
    rows_by_child = existing or {}

    async def _list_relations(*, resource, relation):
        return tuple(rows_by_child.get(resource.resource_id, ()))

    permissions.list_relations = AsyncMock(side_effect=_list_relations)
    return patch(
        "bisheng.permission.application.get_permission_relation_api",
        new=AsyncMock(return_value=permissions),
    )


def _parent_edge(subject_id: str, object_id: str):
    from bisheng.permission.application import (
        PermissionObject,
        PermissionRelation,
        PermissionSubject,
    )

    return PermissionRelation(
        subject=PermissionSubject("department", subject_id),
        relation="parent",
        resource=PermissionObject("department", object_id),
    )


@pytest.mark.asyncio
class TestRun:
    async def test_empty_tree_is_noop(self):
        with _patch_edges([]), _patch_writer() as writer:
            rc = await mod.run(apply=True)
        assert rc == 0
        writer.assert_not_awaited()

    async def test_dry_run_does_not_write(self):
        with _patch_edges([(5, 1), (9, 5)]), _patch_writer() as writer:
            rc = await mod.run(apply=False)
        assert rc == 0
        writer.assert_not_awaited()

    async def test_apply_writes_one_parent_edge_per_child(self):
        with _patch_edges([(5, 1), (9, 5)]), _patch_writer() as writer:
            rc = await mod.run(apply=True)
        assert rc == 0
        writer.assert_awaited_once()
        permissions = writer.return_value
        permissions.apply_changes.assert_awaited_once()
        changes = permissions.apply_changes.await_args.args[0]
        assert [
            (
                change.action,
                change.relation.subject.subject_id,
                change.relation.relation,
                change.relation.resource.resource_id,
            )
            for change in changes
        ] == [
            ("grant", "1", "parent", "5"),
            ("grant", "5", "parent", "9"),
        ]

    async def test_apply_skips_edges_already_in_openfga(self):
        with _patch_edges([(5, 1), (9, 5)]), _patch_writer(
            {"5": (_parent_edge("1", "5"),)}
        ) as writer:
            rc = await mod.run(apply=True)
        assert rc == 0
        permissions = writer.return_value
        permissions.apply_changes.assert_awaited_once()
        changes = permissions.apply_changes.await_args.args[0]
        assert [
            (change.relation.subject.subject_id, change.relation.resource.resource_id)
            for change in changes
        ] == [("5", "9")]

    async def test_apply_does_not_write_when_all_edges_exist(self):
        with _patch_edges([(5, 1)]), _patch_writer(
            {"5": (_parent_edge("1", "5"),)}
        ) as writer:
            rc = await mod.run(apply=True)
        assert rc == 0
        permissions = writer.return_value
        permissions.apply_changes.assert_not_awaited()
