"""Tenant, identity and consent entities.

`Tenant` and `Role` are platform-level (not tenant-owned — a tenant obviously cannot own
itself, and roles are a shared fixed vocabulary). `User` is also platform-level: the same
person may belong to multiple tenants, so a user's identity is not scoped to one tenant —
only their *membership* is (`UserTenantMembership`, which is tenant-owned).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermes_rpt.common.db import Base
from hermes_rpt.common.mixins import TenantOwnedMixin, TimestampMixin, UUIDPKMixin
from hermes_rpt.tenants.enums import ConsentStatus, RoleName


class Tenant(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}


class User(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Platform-admin (cross-tenant) capability lives here, deliberately outside any single
    # tenant's membership rows — see hermes_rpt.tenants.enums.RoleName docstring.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}


class Role(Base, UUIDPKMixin, TimestampMixin):
    """Fixed, seeded reference table (see migrations/versions for the seed data) backing
    `RoleName`. Modelled as a table rather than a bare enum column so membership rows can FK
    to it and so role metadata (description) has one place to live."""

    __tablename__ = "roles"

    name: Mapped[RoleName] = mapped_column(String(50), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")


class UserTenantMembership(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    """A user's role within exactly one tenant. Tenant-owned: this row cannot exist without a
    `tenant_id`, and a user with memberships in three tenants has three separate rows."""

    __tablename__ = "user_tenant_memberships"
    __table_args__ = (
        # A given user can hold a given role at most once within a given tenant; they may
        # still hold multiple *different* roles in the same tenant via multiple rows.
        {"sqlite_autoincrement": False},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped[User] = relationship(lazy="joined")
    role: Mapped[Role] = relationship(lazy="joined")


class DataAccessPolicy(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    """Tenant-defined rules about which roles may access which data classification labels /
    ontology fields. Consumed by Phase 3 authorization and Phase 8 feature extraction; this
    phase only establishes the persisted shape."""

    __tablename__ = "data_access_policies"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    policy_document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}


class DataUsageConsent(Base, UUIDPKMixin, TimestampMixin, TenantOwnedMixin):
    """Explicit, revocable permission for a tenant's data to be used beyond that tenant's own
    models — e.g. in an approved shared-pretraining dataset class (Phase 12). No training
    process may use more than one tenant's data without a corresponding GRANTED row here
    (docs/IMPLEMENTATION_PLAN.md invariant #8)."""

    __tablename__ = "data_usage_consents"

    consent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[ConsentStatus] = mapped_column(
        String(20), nullable=False, default=ConsentStatus.GRANTED
    )
    granted_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    notes: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
