"""Integration test for PostgreSQL Row-Level Security (defense in depth).

Requires a real, migrated PostgreSQL control-plane database (`make up && make migrate`) — the
in-memory SQLite fixtures used by tests/unit and tests/security cannot exercise RLS, since it's
PostgreSQL-specific (docs/adr/0003-tenant-isolation-defense-in-depth.md). Skipped automatically
if no reachable control-plane database is configured, so `make test-unit` / CI's non-integration
job never depends on Docker being up.

This test deliberately does **not** apply `tenant_scoped_session`'s `SET LOCAL` for the "wrong"
tenant to prove isolation — instead it proves the *forgot the filter* scenario: querying with
no `tenant_id` predicate at all still returns zero rows for a session bound to a different
tenant, which is exactly the bug class RLS exists to catch as a second layer.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from hermes_rpt.common.settings import get_settings

pytestmark = pytest.mark.asyncio


async def _postgres_available() -> bool:
    try:
        engine = create_async_engine(str(get_settings().database_url))
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - availability probe, any failure means "skip"
        return False


@pytest.fixture
async def require_postgres() -> None:
    if not await _postgres_available():
        pytest.skip("No reachable PostgreSQL control-plane database (run `make up` first)")


async def test_forgetting_the_tenant_filter_still_returns_no_cross_tenant_rows(
    require_postgres: None,
) -> None:
    engine = create_async_engine(str(get_settings().database_url))
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    async with engine.begin() as conn:
        # `tenants` itself isn't RLS-protected (no tenant_id column — it *is* a tenant, not
        # tenant-owned), but `data_access_policies` is, and FORCE ROW LEVEL SECURITY applies
        # even to the connecting role inserting its own tenant's row — so each insert needs its
        # own tenant bound first via set_config(), not `SET LOCAL ... = :tid` (PostgreSQL's
        # SET/SET LOCAL commands don't accept a bind-parameter placeholder for the value under
        # asyncpg's prepared-statement protocol; fails with "syntax error at or near '$1'").
        # See hermes_rpt.common.tenant_session for the same fix applied to the app's own binding.
        for tid, slug in ((tenant_a, "rls-test-alpha"), (tenant_b, "rls-test-beta")):
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, name, slug, is_active, version) "
                    "VALUES (:id, :slug, :slug, true, 1)"
                ),
                {"id": tid, "slug": slug},
            )
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"), {"tid": str(tid)}
            )
            await conn.execute(
                text(
                    "INSERT INTO data_access_policies "
                    "(id, tenant_id, name, description, policy_document, is_active, version) "
                    "VALUES (:id, :tenant_id, 'rls test policy', '', '{}', true, 1)"
                ),
                {"id": uuid.uuid4(), "tenant_id": tid},
            )

        await conn.execute(
            text("SELECT set_config('app.current_tenant_id', :tid, true)"), {"tid": str(tenant_a)}
        )
        # No tenant_id predicate at all — this is the "someone forgot the filter" scenario.
        result = await conn.execute(text("SELECT tenant_id FROM data_access_policies"))
        visible_tenant_ids = {row[0] for row in result.fetchall()}

        # Cleanup within the same transaction so the test is self-contained. Each tenant's
        # data_access_policies row must be deleted under its own bound context — RLS's USING
        # clause applies to DELETE too, so tenant_a's context (still bound from the query above)
        # can only delete tenant_a's row.
        for tid in (tenant_a, tenant_b):
            await conn.execute(
                text("SELECT set_config('app.current_tenant_id', :tid, true)"), {"tid": str(tid)}
            )
            await conn.execute(
                text("DELETE FROM data_access_policies WHERE tenant_id = :tid"), {"tid": tid}
            )
        await conn.execute(
            text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
        )

    await engine.dispose()

    assert visible_tenant_ids == {tenant_a}
