"""add drift tracking columns to schema_snapshots

Adds the drift-comparison columns Phase 5 needs: `drift_summary` (the computed diff against the
previous completed snapshot for the same connection), and `drift_acknowledged_at` /
`drift_acknowledged_by_principal_id` (Phase 5's "acknowledge drift" API — acknowledging never
auto-rewrites a mapping, it only records that a human reviewed the drift).

Revision ID: 7f7e83e4e8e7
Revises: 91b14d4e87dc
Create Date: 2026-07-27 11:44:54.470020

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f7e83e4e8e7"
down_revision: str | None = "91b14d4e87dc"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("schema_snapshots", sa.Column("drift_summary", sa.JSON(), nullable=True))
    op.add_column(
        "schema_snapshots",
        sa.Column("drift_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "schema_snapshots",
        sa.Column("drift_acknowledged_by_principal_id", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schema_snapshots", "drift_acknowledged_by_principal_id")
    op.drop_column("schema_snapshots", "drift_acknowledged_at")
    op.drop_column("schema_snapshots", "drift_summary")
