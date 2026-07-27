"""Model registry entities.

`ModelVersion.tenant_id` is nullable by design: `NULL` means a shared base model (e.g. a
Hermes-RPT base or a baseline trained on an approved shared/synthetic dataset); a non-null
value means a fully tenant-private model. Because of that, `ModelVersion` deliberately does
**not** use `TenantOwnedMixin` (which forces `tenant_id` to be non-null) — see
`hermes_rpt.registry.repository.ModelVersionRepository` for how visibility is scoped instead.

`TenantModelAdapter` is always tenant-owned — a tenant adapter belongs to exactly one tenant by
construction (Phase 14 requirement), and shared base models must never embed adapter weights.

`stage` uses `Enum(ModelStage, native_enum=False, ...)`, not a plain `String` column — a plain
`String` column stores/reads a bare Python `str`, and since `ModelStage` is a `StrEnum`, most
comparisons (`==`, `in`, dict-key lookup) still silently succeed against that bare string, which
let a real bug (`hermes_rpt.registry.service.ModelRegistryService.transition_stage` calling
`.value` on a plain `str` after certain DB round-trips) hide until an actual end-to-end test
exercised it (Phase 13). `native_enum=False` keeps the on-disk representation identical (VARCHAR,
no DDL/migration change) while making SQLAlchemy always convert back to `ModelStage` on read.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, Uuid
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
        Enum(ModelStage, native_enum=False, length=20, validate_strings=True),
        nullable=False,
        default=ModelStage.CANDIDATE,
    )
    artifact_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    artifact_checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ontology_version: Mapped[str] = mapped_column(String(50), nullable=False)
    feature_contract_version: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}


class ModelAlias(Base, UUIDPKMixin, TimestampMixin):
    """A named, atomically-repointable pointer to exactly one `(ModelVersion, TenantModelAdapter
    | None)` pair for a task — e.g. `"production"` (Phase 14). `tenant_id IS NULL` means a
    shared/platform-level alias (visible to every tenant, same nullable-shared convention as
    `ModelVersion.tenant_id`); a non-null value scopes the alias to one tenant, letting a tenant
    point `"production"` at the shared base model *plus their own private adapter* without
    touching any other tenant's alias.

    This exists alongside `ModelVersion.stage` (Phase 10/13's `PRODUCTION` stage, still how the
    baseline-serving path in `hermes_rpt.inference.service` resolves a model) rather than
    replacing it: `stage` alone cannot express "which adapter goes with this base model for this
    tenant," and cannot express an atomic rollback when more than one `PRODUCTION`-staged row
    could otherwise match. `hermes_rpt.registry.service.ModelRegistryService.set_alias` is the
    only way to point or repoint one; uniqueness of `(tenant_id, task_definition_id, alias_name)`
    is enforced there (an upsert), not by a DB constraint — Postgres does not treat `NULL`
    tenant_id values as equal for uniqueness purposes, which would silently let more than one
    shared alias of the same name past a naive constraint.
    """

    __tablename__ = "model_aliases"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    task_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prediction_task_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    alias_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_model_adapter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenant_model_adapters.id", ondelete="RESTRICT"),
        nullable=True,
    )
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
        Enum(ModelStage, native_enum=False, length=20, validate_strings=True),
        nullable=False,
        default=ModelStage.CANDIDATE,
    )
    artifact_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    artifact_checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    ontology_version: Mapped[str] = mapped_column(String(50), nullable=False)
    feature_contract_version: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}
