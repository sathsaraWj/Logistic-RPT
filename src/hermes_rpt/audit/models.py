"""AuditEvent: the record behind "all access must be auditable"
(docs/IMPLEMENTATION_PLAN.md invariant #10).

`tenant_id` is nullable — deliberately, and unlike almost every other entity in this phase —
because some events genuinely precede tenant resolution (e.g. an authentication failure before
any claim could be verified) or are platform-level (Platform Admin actions spanning tenants).
Every event with a non-null `tenant_id` is exactly as tenant-scoped as any other tenant-owned
row; every event with a null `tenant_id` is visible only to Platform Admin (Phase 3
authorization), never surfaced through a tenant-scoped audit-read endpoint.
`details` must never contain secrets — the same redaction rule as logging applies conceptually,
though this is a structured DB column rather than a log line (see hermes_rpt.common.logging).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.common.db import Base
from hermes_rpt.common.mixins import TimestampMixin, UUIDPKMixin


class AuditEvent(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    principal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown")
    action: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    outcome: Mapped[AuditOutcome] = mapped_column(String(20), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
