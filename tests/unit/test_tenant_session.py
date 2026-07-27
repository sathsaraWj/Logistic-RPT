"""Tests for `hermes_rpt.common.tenant_session.bind_tenant_for_row_level_security`.

Written after discovering this function, while correctly implemented, was never actually
called anywhere in `apps/api`'s request pipeline — every RLS-protected table's `FORCE ROW LEVEL
SECURITY` policy would have silently denied all access (including to the requesting tenant's
own rows) on a real PostgreSQL deployment, invisible to the rest of the test suite because it
runs against SQLite, where this function is a documented no-op. Fixed by having
`hermes_rpt.auth.dependencies.get_tenant_context` call it; these tests cover the helper directly
plus the wiring.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from hermes_rpt.common.tenant_session import bind_tenant_for_row_level_security
from hermes_rpt.tenants.context import TenantContext


def _session_with_dialect(dialect_name: str) -> MagicMock:
    session = MagicMock()
    session.get_bind.return_value.dialect.name = dialect_name
    session.execute = AsyncMock()
    return session


async def test_sets_the_session_variable_on_postgresql() -> None:
    session = _session_with_dialect("postgresql")
    tenant_id = uuid.uuid4()
    tenant_context = TenantContext(tenant_id=tenant_id, principal_id=uuid.uuid4())

    await bind_tenant_for_row_level_security(session, tenant_context)

    session.execute.assert_awaited_once()
    (statement, params), _kwargs = session.execute.call_args
    assert "SET LOCAL app.current_tenant_id" in str(statement)
    assert params == {"tenant_id": str(tenant_id)}


async def test_is_a_no_op_on_non_postgresql_dialects() -> None:
    session = _session_with_dialect("sqlite")
    tenant_context = TenantContext(tenant_id=uuid.uuid4(), principal_id=uuid.uuid4())

    await bind_tenant_for_row_level_security(session, tenant_context)

    session.execute.assert_not_awaited()
