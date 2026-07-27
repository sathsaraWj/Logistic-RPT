"""add model_aliases

Phase 14's atomically-repointable named pointer to a `(ModelVersion, TenantModelAdapter | None)`
pair for a task — see `hermes_rpt.registry.models.ModelAlias` for the full rationale (why this
exists alongside `ModelVersion.stage`, and why uniqueness of `(tenant_id, task_definition_id,
alias_name)` is enforced in the service layer rather than as a DB constraint).

`tenant_id` is nullable-shared, same RLS treatment as `model_versions` (see
91b14d4e87dc_enable_row_level_security.py) — extended here rather than in that migration since
this table didn't exist yet at that point.

Revision ID: 995d4a06ea5c
Revises: 7f7e83e4e8e7
Create Date: 2026-07-27 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "995d4a06ea5c"
down_revision: str | None = "7f7e83e4e8e7"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("task_definition_id", sa.Uuid(), nullable=False),
        sa.Column("alias_name", sa.String(100), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_model_adapter_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["task_definition_id"], ["prediction_task_definitions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["tenant_model_adapter_id"], ["tenant_model_adapters.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_model_aliases_tenant_id", "model_aliases", ["tenant_id"])

    op.execute("ALTER TABLE model_aliases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE model_aliases FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON model_aliases "
        "USING (tenant_id IS NULL OR tenant_id = hermes_current_tenant_id())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON model_aliases")
    op.execute("ALTER TABLE model_aliases NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE model_aliases DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_model_aliases_tenant_id", table_name="model_aliases")
    op.drop_table("model_aliases")
