"""Model registry entities.

`ModelVersion.tenant_id` is nullable by design: `NULL` means a shared base model (e.g. a
Hermes-RPT base or a baseline trained on an approved shared/synthetic dataset); a non-null
value means a fully tenant-private model. Because of that, `ModelVersion` deliberately does
**not** use `TenantOwnedMixin` (which forces `tenant_id` to be non-null) — see
`hermes_rpt.registry.repository.ModelVersionRepository` for how visibility is scoped instead.

`TenantModelAdapter` is always tenant-owned — a tenant adapter belongs to exactly one tenant by
construction (Phase 14 requirement), and shared base models must never embed adapter weights.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hermes_rpt.common.db import Base
from hermes_rpt.common.mixins import TenantOwnedMixin, TimestampMixin, UUIDPKMixin
from hermes_rpt.registry.enums import ModelStage


class ModelVersion(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "model_versions"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version_label: Mapped[str] = mapped_column(String(50), nullable=False)
    task_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prediction_task_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stage: Mapped[ModelStage] = mapped_column(
        String(20), nullable=False, default=ModelStage.CANDIDATE
    )
    artifact_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    artifact_checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ontology_version: Mapped[str] = mapped_column(String(50), nullable=False)
    feature_contract_version: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}


class TenantModelAdapter(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "tenant_model_adapters"

    base_model_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version_label: Mapped[str] = mapped_column(String(50), nullable=False)
    stage: Mapped[ModelStage] = mapped_column(
        String(20), nullable=False, default=ModelStage.CANDIDATE
    )
    artifact_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    artifact_checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    ontology_version: Mapped[str] = mapped_column(String(50), nullable=False)
    feature_contract_version: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}
