"""enable row level security (defense in depth)

Enables PostgreSQL Row-Level Security on every tenant-owned table and adds a policy keyed off
the `app.current_tenant_id` session variable that `hermes_rpt.common.tenant_session` sets via
`SET LOCAL` at the start of a request-scoped transaction.

This is explicitly **defense in depth**, not the primary control — see
docs/adr/0003-tenant-isolation-defense-in-depth.md. The application-layer filtering in
`hermes_rpt.common.repository.TenantScopedRepository` is what the platform actually relies on;
RLS exists so that a bug which forgot that filter still cannot leak another tenant's rows,
*provided* the session correctly set `app.current_tenant_id` — which is itself an assumption
this defense-in-depth layer depends on, not a guarantee independent of application code.

Uses `FORCE ROW LEVEL SECURITY` so policies apply even to the table-owning role the application
connects as (by default PostgreSQL RLS does not restrict the table owner).

Postgres-only: this migration will fail against any other database engine. That is intentional
— the Hermes-RPT control-plane database is always PostgreSQL (see
hermes_rpt.common.settings.Settings.database_url).

Revision ID: 91b14d4e87dc
Revises: 96b4009ff8d0
Create Date: 2026-07-27 10:57:12.118121

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "91b14d4e87dc"
down_revision: str | None = "96b4009ff8d0"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Tables where tenant_id is NOT NULL (TenantOwnedMixin) — a row is visible only to its owning
# tenant's session.
_STRICT_TENANT_TABLES = [
    "user_tenant_memberships",
    "data_access_policies",
    "data_usage_consents",
    "database_credential_references",
    "customer_database_connections",
    "schema_snapshots",
    "schema_mappings",
    "mapping_versions",
    "prediction_requests",
    "prediction_results",
    "tenant_model_adapters",
]

# Tables where tenant_id is nullable and NULL means "shared / platform-level" — those rows
# remain visible to every tenant session (e.g. a shared base ModelVersion), in addition to a
# tenant's own rows.
_NULLABLE_SHARED_TABLES = ["model_versions"]

# audit_events also has a nullable tenant_id, but NULL there means "platform-level event," which
# must NOT be visible to an ordinary tenant session — so it gets the strict policy even though
# the column itself is nullable.
_STRICT_NULLABLE_TABLES = ["audit_events"]

_HELPER_FUNCTION = """
CREATE FUNCTION hermes_current_tenant_id() RETURNS uuid AS $$
    SELECT nullif(current_setting('app.current_tenant_id', true), '')::uuid;
$$ LANGUAGE sql STABLE;
"""


def upgrade() -> None:
    op.execute(_HELPER_FUNCTION)

    for table in _STRICT_TENANT_TABLES + _STRICT_NULLABLE_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = hermes_current_tenant_id())"
        )

    for table in _NULLABLE_SHARED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id IS NULL OR tenant_id = hermes_current_tenant_id())"
        )


def downgrade() -> None:
    for table in _STRICT_TENANT_TABLES + _STRICT_NULLABLE_TABLES + _NULLABLE_SHARED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS hermes_current_tenant_id()")
