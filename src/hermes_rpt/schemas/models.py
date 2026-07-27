"""SchemaSnapshot: a versioned, fingerprinted record of what schema discovery (Phase 5) found
in a tenant's customer database. Phase 2 defined the persisted shape; Phase 5 adds the
drift-tracking columns, the discovery job itself, the fingerprint algorithm, and drift
comparison.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hermes_rpt.common.db import Base
from hermes_rpt.common.mixins import TenantOwnedMixin, TimestampMixin, UUIDPKMixin
from hermes_rpt.schemas.enums import DiscoveryStatus


class SchemaSnapshot(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "schema_snapshots"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customer_database_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DiscoveryStatus] = mapped_column(
        String(20), nullable=False, default=DiscoveryStatus.PENDING
    )
    schema_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    table_fingerprints: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Drift relative to the previous COMPLETED snapshot for the same connection, computed once
    # this snapshot finishes (hermes_rpt.schemas.drift.compare_snapshots). Null on a
    # connection's first snapshot — there is nothing to compare against yet.
    drift_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    drift_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    drift_acknowledged_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    __table_args__ = (
        # A tenant's connection cannot have two snapshots claiming the same sequence number —
        # scoped uniqueness per Phase 2 requirement 7.
        UniqueConstraint(
            "tenant_id", "connection_id", "sequence_number", name="uq_schema_snapshot_sequence"
        ),
    )
