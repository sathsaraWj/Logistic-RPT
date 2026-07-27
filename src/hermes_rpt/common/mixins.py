"""Shared ORM mixins.

`TenantOwnedMixin` is the mechanical half of the tenant-isolation invariant: every table that
mixes it in gets a mandatory, indexed `tenant_id` column. The other half — actually filtering
by it on every query — lives in `hermes_rpt.common.repository.TenantScopedRepository`, never
left to individual call sites to remember. See docs/adr/0003-tenant-isolation-defense-in-depth.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPKMixin:
    """Internal primary key. Per Phase 2 requirements, all entities use UUIDs internally —
    never a database-sequential integer that could leak row-count/order information."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantOwnedMixin:
    """Mixed into every tenant-owned table. `tenant_id` is intentionally never nullable —
    a tenant-owned row with no tenant is exactly the "missing tenant context" case the
    platform is required to fail closed on (docs/IMPLEMENTATION_PLAN.md invariant #11)."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
