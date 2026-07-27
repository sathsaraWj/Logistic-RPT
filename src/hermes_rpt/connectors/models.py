"""Customer database connection metadata.

Only metadata lives here — host, port, database name, an opaque credential *reference*, and a
schema/table allowlist. The resolved secret is never stored in the control plane at all (see
docs/adr/0005-secret-provider-abstraction.md); `DatabaseCredentialReference.reference_key` is
meaningless outside the `SecretProvider` that issued it. Connection pooling, read-only
enforcement, and timeouts are runtime behaviour added in Phase 4 — this phase only defines the
persisted shape and its tenant ownership.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hermes_rpt.common.db import Base
from hermes_rpt.common.mixins import TenantOwnedMixin, TimestampMixin, UUIDPKMixin
from hermes_rpt.connectors.enums import ConnectionStatus, DatabaseEngine


class DatabaseCredentialReference(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "database_credential_references"

    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    reference_key: Mapped[str] = mapped_column(String(500), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomerDatabaseConnection(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    __tablename__ = "customer_database_connections"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    engine: Mapped[DatabaseEngine] = mapped_column(String(20), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database_name: Mapped[str] = mapped_column(String(200), nullable=False)
    username: Mapped[str] = mapped_column(String(200), nullable=False)
    tls_mode: Mapped[str] = mapped_column(String(50), nullable=False, default="require")

    credential_reference_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("database_credential_references.id", ondelete="RESTRICT"),
        nullable=False,
    )

    status: Mapped[ConnectionStatus] = mapped_column(
        String(30), nullable=False, default=ConnectionStatus.PENDING_VALIDATION
    )
    schema_allowlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    table_allowlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}
