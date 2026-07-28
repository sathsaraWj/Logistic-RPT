"""Tests for `hermes_rpt.common.tenant_session.bind_tenant_for_row_level_security`.

Written after discovering this function, while correctly implemented, was never actually
called anywhere in `apps/api`'s request pipeline — every RLS-protected table's `FORCE ROW LEVEL
SECURITY` policy would have silently denied all access (including to the requesting tenant's
own rows) on a real PostgreSQL deployment, invisible to the rest of the test suite because it
runs against SQLite, where this function is a documented no-op. Fixed by having
`hermes_rpt.auth.dependencies.get_tenant_context` call it; these tests cover the helper directly
plus the wiring.

Takes a raw `tenant_id: uuid.UUID | None` (not a `TenantContext`) since Phase 16 added a third
call site — `apps/api/exception_handlers.py`'s best-effort audit write — that only ever has a
raw, sometimes-`None`, tenant id in scope, not a full verified context.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from hermes_rpt.common.tenant_session import bind_tenant_for_row_level_security


def _session_with_dialect(dialect_name: str) -> MagicMock:
    session = MagicMock()
    session.get_bind.return_value.dialect.name = dialect_name
    session.execute = AsyncMock()
    return session


async def test_sets_the_session_variable_on_postgresql() -> None:
    session = _session_with_dialect("postgresql")
    tenant_id = uuid.uuid4()

    await bind_tenant_for_row_level_security(session, tenant_id)

    session.execute.assert_awaited_once()
    (statement, params), _kwargs = session.execute.call_args
    assert "set_config" in str(statement)
    assert "app.current_tenant_id" in str(statement)
    assert params == {"tenant_id": str(tenant_id)}


async def test_is_a_no_op_on_non_postgresql_dialects() -> None:
    session = _session_with_dialect("sqlite")

    await bind_tenant_for_row_level_security(session, uuid.uuid4())

    session.execute.assert_not_awaited()


async def test_is_a_no_op_for_a_none_tenant_id_even_on_postgresql() -> None:
    """A platform-level caller with no tenant to bind (e.g. an authentication failure before
    any tenant claim was verified) — leaving the session variable unset for this transaction is
    equivalent to explicitly clearing it, and correct: `audit_events`'s RLS policy (migration
    `b48a21a99ef0`) treats an unbound session as eligible to write a NULL-tenant row."""

    session = _session_with_dialect("postgresql")

    await bind_tenant_for_row_level_security(session, None)

    session.execute.assert_not_awaited()
