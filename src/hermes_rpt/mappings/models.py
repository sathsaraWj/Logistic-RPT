"""Schema-to-ontology mapping entities.

`SchemaMapping` is the stable identity for "how Tenant X's `Vehicle` concept maps to the
canonical ontology"; `MappingVersion` holds the actual (immutable once active — Phase 7
requirement 4) declarative mapping document for one version of it. Phase 2 only defines the
persisted shape; the mapping language, suggestion engine, and lifecycle transitions are Phase 7.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hermes_rpt.common.db import Base
from hermes_rpt.common.mixins import TenantOwnedMixin, TimestampMixin, UUIDPKMixin
from hermes_rpt.mappings.enums import MappingState


class SchemaMapping(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "schema_mappings"

    entity_name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("schema_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    ontology_version: Mapped[str] = mapped_column(String(50), nullable=False)
    state: Mapped[MappingState] = mapped_column(
        String(30), nullable=False, default=MappingState.DRAFT
    )
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "mapping_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_schema_mapping_active_version",
        ),
        nullable=True,
    )
    suspended_due_to_drift: Mapped[bool] = mapped_column(nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}


class MappingVersion(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "mapping_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "schema_mapping_id", "version_number", name="uq_mapping_version_number"
        ),
    )

    schema_mapping_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("schema_mappings.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    mapping_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    is_ai_suggested: Mapped[bool] = mapped_column(nullable=False, default=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    explanation: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
