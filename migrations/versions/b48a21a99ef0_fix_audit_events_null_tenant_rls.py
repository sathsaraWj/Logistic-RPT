"""fix audit_events RLS policy to actually admit NULL-tenant rows

Phase 16 security review finding: `audit_events`'s RLS policy from `91b14d4e87dc` is
`USING (tenant_id = hermes_current_tenant_id())`, applied to both reads and writes (no separate
`WITH CHECK` was given, so PostgreSQL reuses `USING` for both) under `FORCE ROW LEVEL SECURITY`.
In SQL, `NULL = NULL` evaluates to `NULL`, not `TRUE` — RLS treats that as "denied," the same as
`FALSE`. So a platform-level audit event (`tenant_id IS NULL`, e.g. an authentication failure
before any tenant was ever resolved — see `apps/api/exception_handlers.py`'s
`authentication_error_handler`) could **never** be inserted on real PostgreSQL, no matter
whether `app.current_tenant_id` was bound or not: every pre-auth-failure audit write was
silently rejected the moment this migration's policy took effect, with no error surfacing
anywhere the platform team would see it (`_record_audit_event_best_effort` swallows the
exception by design — see that module's docstring — logging only `audit_event_write_failed`).

Fix: an explicit `WITH CHECK` (and matching `USING`) that also admits the case where both sides
are NULL — a session with no bound tenant inserting a NULL-tenant row. Read visibility is
unchanged: a tenant session (a real `hermes_current_tenant_id()`) still never sees another
tenant's rows, and still never sees platform-level (NULL-tenant) rows either, since
`tenant_id IS NULL AND hermes_current_tenant_id() IS NULL` is false whenever a tenant *is* bound.

Revision ID: b48a21a99ef0
Revises: 978402aae411
Create Date: 2026-07-28 02:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b48a21a99ef0"
down_revision: str | None = "978402aae411"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_POLICY_EXPRESSION = (
    "(tenant_id = hermes_current_tenant_id()) "
    "OR (tenant_id IS NULL AND hermes_current_tenant_id() IS NULL)"
)


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON audit_events")
    op.execute(
        f"CREATE POLICY tenant_isolation ON audit_events "
        f"USING ({_POLICY_EXPRESSION}) WITH CHECK ({_POLICY_EXPRESSION})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON audit_events")
    op.execute(
        "CREATE POLICY tenant_isolation ON audit_events "
        "USING (tenant_id = hermes_current_tenant_id())"
    )
