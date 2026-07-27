"""Service layer for tenant/identity operations.

Services are the layer API handlers are actually allowed to call (Phase 2 requirement 3).
They exist so business rules — "tenant context is mandatory," "reject mismatched tenant and
resource IDs," "membership requires an existing role" — live in exactly one place rather than
being re-decided in every handler.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.common.repository import TenantContextRequiredError, TenantMismatchError
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.enums import RoleName
from hermes_rpt.tenants.models import Tenant, User, UserTenantMembership
from hermes_rpt.tenants.repository import (
    RoleRepository,
    TenantRepository,
    UserRepository,
    UserTenantMembershipRepository,
)


class TenantService:
    """Platform-level tenant management. Callers of these methods must themselves be
    authorized as Platform Admin (Phase 3 enforces this at the API boundary) — there is no
    TenantContext parameter here because a tenant cannot be tenant-scoped to itself."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tenants = TenantRepository(session)

    async def create_tenant(self, *, name: str, slug: str) -> Tenant:
        tenant = Tenant(name=name, slug=slug)
        return await self._tenants.add(tenant)

    async def get_tenant(self, tenant_id: uuid.UUID) -> Tenant | None:
        return await self._tenants.get(tenant_id)


class MembershipService:
    """Assigns tenant-scoped roles to users. Every method requires a `TenantContext` and
    refuses to operate on a membership belonging to a different tenant — this is the concrete
    behaviour the Phase 2 negative security tests exercise."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._roles = RoleRepository(session)
        self._memberships = UserTenantMembershipRepository(session)

    async def add_membership(
        self,
        *,
        user_id: uuid.UUID,
        role: RoleName,
        tenant_context: TenantContext,
    ) -> UserTenantMembership:
        role_row = await self._roles.get_by_name(role)
        if role_row is None:
            raise ValueError(f"Unknown role {role!r} — roles must be seeded via migration")

        membership = UserTenantMembership(
            tenant_id=tenant_context.tenant_id,
            user_id=user_id,
            role_id=role_row.id,
        )
        return await self._memberships.add(membership, tenant_context=tenant_context)

    async def get_membership(
        self, membership_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> UserTenantMembership:
        """Raises TenantMismatchError (surfaced as 404, never 403, at the API boundary) if the
        membership belongs to a different tenant than tenant_context — see
        hermes_rpt.common.repository.TenantMismatchError."""

        return await self._memberships.require(membership_id, tenant_context=tenant_context)

    async def list_memberships(
        self, *, tenant_context: TenantContext
    ) -> list[UserTenantMembership]:
        return await self._memberships.list_for_tenant(tenant_context=tenant_context)


__all__ = [
    "MembershipService",
    "TenantContextRequiredError",
    "TenantMismatchError",
    "TenantService",
    "User",
]
