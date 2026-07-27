"""Happy-path tests for the tenant/identity service layer added in Phase 2."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.enums import RoleName
from hermes_rpt.tenants.models import Role
from hermes_rpt.tenants.repository import TenantRepository, UserRepository
from hermes_rpt.tenants.service import MembershipService, TenantService
from tests.factories import make_tenant, make_user


async def test_create_tenant(session: AsyncSession) -> None:
    service = TenantService(session)
    tenant = await service.create_tenant(name="Acme Logistics", slug="acme-logistics")

    assert tenant.id is not None
    assert (await TenantRepository(session).get_by_slug("acme-logistics")) is tenant


async def test_add_and_fetch_membership(
    session: AsyncSession, seeded_roles: dict[RoleName, Role]
) -> None:
    tenant = await TenantRepository(session).add(make_tenant())
    user = await UserRepository(session).add(make_user())
    context = TenantContext(tenant_id=tenant.id, principal_id=user.id)

    membership_service = MembershipService(session)
    membership = await membership_service.add_membership(
        user_id=user.id, role=RoleName.TENANT_ADMIN, tenant_context=context
    )

    fetched = await membership_service.get_membership(membership.id, tenant_context=context)
    assert fetched.id == membership.id
    assert fetched.role.name == RoleName.TENANT_ADMIN
    assert fetched.tenant_id == tenant.id


async def test_add_membership_rejects_unknown_role(session: AsyncSession) -> None:
    tenant = await TenantRepository(session).add(make_tenant())
    user = await UserRepository(session).add(make_user())
    context = TenantContext(tenant_id=tenant.id, principal_id=user.id)

    with pytest.raises(ValueError, match="Unknown role"):
        await MembershipService(session).add_membership(
            user_id=user.id, role=RoleName.TENANT_ADMIN, tenant_context=context
        )
