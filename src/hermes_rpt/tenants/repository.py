"""Repositories for the tenant/identity bounded context."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, select

from hermes_rpt.common.repository import BaseRepository, TenantScopedRepository
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.enums import ConsentStatus, RoleName
from hermes_rpt.tenants.models import (
    DataAccessPolicy,
    DataUsageConsent,
    Role,
    Tenant,
    User,
    UserTenantMembership,
)


class TenantRepository(BaseRepository[Tenant]):
    model = Tenant

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self.session.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


class RoleRepository(BaseRepository[Role]):
    model = Role

    async def get_by_name(self, name: RoleName) -> Role | None:
        result = await self.session.execute(select(Role).where(Role.name == name))
        return result.scalar_one_or_none()


class UserTenantMembershipRepository(TenantScopedRepository[UserTenantMembership]):
    model = UserTenantMembership

    async def list_for_user_in_tenant(
        self, user_id: uuid.UUID, *, tenant_context: TenantContext
    ) -> list[UserTenantMembership]:
        stmt = select(UserTenantMembership).where(
            and_(
                UserTenantMembership.tenant_id == tenant_context.tenant_id,
                UserTenantMembership.user_id == user_id,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class DataAccessPolicyRepository(TenantScopedRepository[DataAccessPolicy]):
    model = DataAccessPolicy


class DataUsageConsentRepository(TenantScopedRepository[DataUsageConsent]):
    model = DataUsageConsent

    async def has_granted_consent(self, tenant_id: uuid.UUID, *, consent_type: str) -> bool:
        """Deliberately not scoped by a caller's own `TenantContext` — checking *another*
        tenant's consent record is exactly what a shared-pretraining consent gate
        (`hermes_rpt.models.transformer.pretraining_consent`) needs to do, one contributing
        tenant at a time."""

        stmt = select(DataUsageConsent).where(
            and_(
                DataUsageConsent.tenant_id == tenant_id,
                DataUsageConsent.consent_type == consent_type,
                DataUsageConsent.status == ConsentStatus.GRANTED,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None
