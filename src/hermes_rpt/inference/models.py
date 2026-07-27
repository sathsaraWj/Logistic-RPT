"""Prediction task registry and per-request/result records.

`PredictionTaskDefinition` is platform-level — it is the catalogue of prediction tasks Hermes-
RPT supports (starting with delivery-delay risk, Phase 8), not owned by any one tenant.
`PredictionRequest` and `PredictionResult` are tenant-owned: every prediction a tenant asks for
and receives is recorded, which is also the backbone of the prediction audit trail (Phase 13).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hermes_rpt.common.db import Base
from hermes_rpt.common.mixins import TenantOwnedMixin, TimestampMixin, UUIDPKMixin
from hermes_rpt.inference.enums import PredictionStatus


class PredictionTaskDefinition(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "prediction_task_definitions"

    task_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    feature_contract_version: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PredictionRequest(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "prediction_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_prediction_request_idempotency"),
    )

    task_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prediction_task_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    business_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    prediction_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[PredictionStatus] = mapped_column(
        String(20), nullable=False, default=PredictionStatus.PENDING
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    requested_by_principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class PredictionResult(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "prediction_results"

    prediction_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prediction_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_model_adapter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenant_model_adapters.id", ondelete="RESTRICT"),
        nullable=True,
    )
    mapping_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_versions.id", ondelete="RESTRICT"), nullable=False
    )
    feature_version: Mapped[str] = mapped_column(String(50), nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    explanations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
