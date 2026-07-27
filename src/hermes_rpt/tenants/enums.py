"""Enumerations for the tenant/identity bounded context."""

from __future__ import annotations

from enum import StrEnum


class RoleName(StrEnum):
    """The fixed role set from docs/IMPLEMENTATION_PLAN.md / prompts.txt Phase 2.

    PLATFORM_ADMIN is included for completeness and reporting, but cross-tenant platform-admin
    capability is granted via `User.is_platform_admin`, never by a tenant-scoped
    UserTenantMembership row — a membership row is always scoped to exactly one tenant, and
    "cross-tenant by role assignment" would undermine that (see
    docs/adr/0003-tenant-isolation-defense-in-depth.md).
    """

    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    DATA_STEWARD = "data_steward"
    ML_ENGINEER = "ml_engineer"
    MANAGER = "manager"
    OPERATOR = "operator"
    READ_ONLY_AUDITOR = "read_only_auditor"
    SERVICE_ACCOUNT = "service_account"


class ConsentStatus(StrEnum):
    GRANTED = "granted"
    REVOKED = "revoked"
    EXPIRED = "expired"
