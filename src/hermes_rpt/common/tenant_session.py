"""Binds the trusted tenant into the database session for PostgreSQL Row-Level Security.

This is defense in depth only (docs/adr/0003-tenant-isolation-defense-in-depth.md) — the
primary control is `hermes_rpt.common.repository.TenantScopedRepository`'s explicit `tenant_id`
filtering, which remains in place regardless of whether this helper is called. RLS policies
(migrations/versions) key off the `app.current_tenant_id` Postgres session variable set here.

A no-op on non-PostgreSQL binds (e.g. the SQLite engine used by fast unit/security tests) since
`SET LOCAL` is PostgreSQL syntax and RLS is not being exercised there anyway.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.tenants.context import TenantContext


async def bind_tenant_for_row_level_security(
    session: AsyncSession, tenant_context: TenantContext
) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # SET LOCAL scopes the variable to the current transaction only, so it can never leak to a
    # different tenant's request when the underlying connection is returned to a pool.
    await session.execute(
        text("SET LOCAL app.current_tenant_id = :tenant_id"),
        {"tenant_id": str(tenant_context.tenant_id)},
    )
