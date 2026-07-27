"""seed roles and delivery-delay-risk prediction task

Seeds the fixed `roles` reference table (hermes_rpt.tenants.enums.RoleName) and registers
`delivery-delay-risk` as the platform's first `PredictionTaskDefinition`
(docs/IMPLEMENTATION_PLAN.md §4 — the smallest viable first prediction task). Its actual
feature contract is defined in Phase 8; this row only reserves the task_key and a placeholder
feature-contract version so later phases have something to reference.

Revision ID: 96b4009ff8d0
Revises: d9d76db9f705
Create Date: 2026-07-27 10:57:09.959357

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "96b4009ff8d0"
down_revision: str | None = "d9d76db9f705"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ROLES = [
    (
        "platform_admin",
        "Cross-tenant platform administration (granted via User.is_platform_admin, "
        "not a tenant membership row).",
    ),
    ("tenant_admin", "Full administrative control within one tenant."),
    ("data_steward", "Owns schema mappings and data access policy decisions for one tenant."),
    ("ml_engineer", "Trains and evaluates models; manages tenant model adapters."),
    ("manager", "Business user consuming predictions and reports."),
    ("operator", "Day-to-day operational use of predictions."),
    ("read_only_auditor", "Read-only access to audit logs and reports for one tenant."),
    ("service_account", "Machine identity acting on behalf of one tenant."),
]

_DELIVERY_DELAY_TASK_KEY = "delivery-delay-risk"


def upgrade() -> None:
    # created_at/updated_at are deliberately omitted from this table proxy so the columns'
    # server_default (now()) fills them in, rather than trying to pass a server-side function
    # call through op.bulk_insert's parameter binding.
    roles_table = sa.table(
        "roles",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
    )
    op.bulk_insert(
        roles_table,
        [
            {"id": uuid.uuid4(), "name": name, "description": description}
            for name, description in _ROLES
        ],
    )

    tasks_table = sa.table(
        "prediction_task_definitions",
        sa.column("id", sa.Uuid()),
        sa.column("task_key", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("feature_contract_version", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        tasks_table,
        [
            {
                "id": uuid.uuid4(),
                "task_key": _DELIVERY_DELAY_TASK_KEY,
                "name": "Delivery delay risk",
                "description": (
                    "Probability that a given trip's delivery will be late relative to its "
                    "planned time. The platform's first prediction task — see "
                    "docs/IMPLEMENTATION_PLAN.md section 4."
                ),
                "feature_contract_version": "unreleased",
                "is_active": False,
            }
        ],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM prediction_task_definitions WHERE task_key = :key").bindparams(
            key=_DELIVERY_DELAY_TASK_KEY
        )
    )
    op.execute(
        sa.text("DELETE FROM roles WHERE name IN :names").bindparams(
            sa.bindparam("names", value=[name for name, _ in _ROLES], expanding=True)
        )
    )
