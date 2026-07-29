"""add service credentials for external partner integrations

Adds `service_credentials` (`hermes_rpt.auth.models.ServiceCredential`) — a per-integration
`client_id`/`secret_hash` pair an external partner (e.g. Hermes VMS) exchanges for a short-lived
service token, independently revocable without touching the platform's global JWT signing
secret. See docs/AUTHENTICATION.md §5a for the full design.

RLS policy carve-out: this table intentionally does NOT get the standard `tenant_id =
hermes_current_tenant_id()` policy every other strict tenant-owned table gets
(91b14d4e87dc). The token-exchange flow (`ServiceCredentialRepository.find_by_client_id`) has
no `TenantContext` yet — resolving *which* tenant a `client_id` belongs to is the entire point
of that lookup, so it necessarily runs with no tenant bound to the session at all. Under the
standard policy, that lookup would see zero rows for every tenant the day RLS actually
constrains queries (it currently doesn't — see the BYPASSRLS finding tracked separately in
docs/SECURITY_REVIEW.md — this carve-out is invisible today but would silently break the
exchange endpoint the moment that finding is fixed if left as the standard policy). The carve-out
below allows an unbound session to see every row (needed for the exchange lookup) while a
session bound to a specific tenant still only sees its own rows. This is not a relaxation of
BYPASSRLS itself — it only changes this one new table's own policy.

Revision ID: f94e2a13d107
Revises: b48a21a99ef0
Create Date: 2026-07-29 21:29:59.569955

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f94e2a13d107"
down_revision: str | None = "b48a21a99ef0"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_TABLE = "service_credentials"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("client_id"),
    )
    op.create_index(f"ix_{_TABLE}_tenant_id", _TABLE, ["tenant_id"])
    op.create_index(f"ix_{_TABLE}_client_id", _TABLE, ["client_id"])

    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLE} USING ("
        f"tenant_id = hermes_current_tenant_id() "
        f"OR current_setting('app.current_tenant_id', true) IS NULL "
        f"OR current_setting('app.current_tenant_id', true) = ''"
        f")"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_index(f"ix_{_TABLE}_client_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_tenant_id", table_name=_TABLE)
    op.drop_table(_TABLE)
