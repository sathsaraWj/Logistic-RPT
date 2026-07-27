"""Binds the trusted tenant into the database session for PostgreSQL Row-Level Security.

This is defense in depth only (docs/adr/0003-tenant-isolation-defense-in-depth.md) — the
primary control is `hermes_rpt.common.repository.TenantScopedRepository`'s explicit `tenant_id`
filtering, which remains in place regardless of whether this helper is called. RLS policies
(migrations/versions) key off the `app.current_tenant_id` Postgres session variable set here.

A no-op on non-PostgreSQL binds (e.g. the SQLite engine used by fast unit/security tests) since
this is PostgreSQL-specific syntax and RLS is not being exercised there anyway.

Uses `set_config('app.current_tenant_id', $1, true)`, not `SET LOCAL app.current_tenant_id =
$1` — PostgreSQL's `SET`/`SET LOCAL` commands do not accept a bind-parameter placeholder for the
value (only a literal), which asyncpg's prepared-statement protocol always uses; that form fails
with `PostgresSyntaxError: syntax error at or near "$1"` on every call, caught only by actually
running this against real PostgreSQL (the rest of the test suite runs against SQLite, where this
function no-ops). `set_config()` is a normal function call and takes its value as a proper bound
parameter; its third argument (`true` = "local", mirroring `SET LOCAL`) gives the identical
"scoped to this transaction only, never leaks across a pooled connection" guarantee.
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
    await session.execute(
        text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_context.tenant_id)},
    )
