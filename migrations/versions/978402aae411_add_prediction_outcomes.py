"""add prediction_outcomes

Phase 15's "precision/recall when labels arrive" — see `hermes_rpt.inference.models.
PredictionOutcome` for the full rationale. Strictly tenant-owned (unlike `model_versions`),
same RLS treatment as `prediction_results`/`prediction_requests`.

Revision ID: 978402aae411
Revises: 995d4a06ea5c
Create Date: 2026-07-28 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "978402aae411"
down_revision: str | None = "995d4a06ea5c"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prediction_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("prediction_result_id", sa.Uuid(), nullable=False),
        sa.Column("actual_label", sa.Boolean(), nullable=False),
        sa.Column("recorded_by_principal_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["prediction_result_id"], ["prediction_results.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("prediction_result_id", name="uq_prediction_outcome_result"),
    )
    op.create_index("ix_prediction_outcomes_tenant_id", "prediction_outcomes", ["tenant_id"])

    op.execute("ALTER TABLE prediction_outcomes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prediction_outcomes FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON prediction_outcomes "
        "USING (tenant_id = hermes_current_tenant_id())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON prediction_outcomes")
    op.execute("ALTER TABLE prediction_outcomes NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE prediction_outcomes DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_prediction_outcomes_tenant_id", table_name="prediction_outcomes")
    op.drop_table("prediction_outcomes")
