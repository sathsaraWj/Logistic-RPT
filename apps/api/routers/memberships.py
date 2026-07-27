"""Tenant membership endpoints.

The first real, auth-protected routes in the platform — deliberately built on top of the
Phase 2 `MembershipService` rather than querying ORM models directly (Phase 2 requirement 3),
and used as the concrete surface for Phase 3's security tests: missing/expired/invalid tokens,
missing scope, tenant/resource mismatch (`GET /{membership_id}` for another tenant's
membership), and service-token-used-as-human-token (`POST /` requires a human principal).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from apps.api.deps import DbSessionDep
from hermes_rpt.auth.dependencies import require_human, require_scopes
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.enums import RoleName
from hermes_rpt.tenants.models import UserTenantMembership
from hermes_rpt.tenants.service import MembershipService

router = APIRouter(prefix="/v1/memberships", tags=["memberships"])


class MembershipResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: RoleName
    is_active: bool

    @classmethod
    def from_orm_membership(cls, membership: UserTenantMembership) -> MembershipResponse:
        return cls(
            id=membership.id,
            tenant_id=membership.tenant_id,
            user_id=membership.user_id,
            role=membership.role.name,
            is_active=membership.is_active,
        )


class MembershipCreateRequest(BaseModel):
    user_id: uuid.UUID
    role: RoleName


@router.get("", response_model=list[MembershipResponse])
async def list_memberships(
    session: DbSessionDep,
    tenant_context: Annotated[TenantContext, Depends(require_scopes(ScopeName.TENANT_READ))],
) -> list[MembershipResponse]:
    memberships = await MembershipService(session).list_memberships(tenant_context=tenant_context)
    return [MembershipResponse.from_orm_membership(m) for m in memberships]


@router.get("/{membership_id}", response_model=MembershipResponse)
async def get_membership(
    membership_id: uuid.UUID,
    session: DbSessionDep,
    tenant_context: Annotated[TenantContext, Depends(require_scopes(ScopeName.TENANT_READ))],
) -> MembershipResponse:
    # MembershipService.get_membership raises TenantMismatchError for another tenant's
    # membership id, which apps.api.exception_handlers maps to a 404 — this is the
    # tenant/resource-mismatch and cross-tenant-object-access behaviour Phase 3 tests exercise.
    membership = await MembershipService(session).get_membership(
        membership_id, tenant_context=tenant_context
    )
    return MembershipResponse.from_orm_membership(membership)


@router.post("", response_model=MembershipResponse, status_code=status.HTTP_201_CREATED)
async def create_membership(
    body: MembershipCreateRequest,
    session: DbSessionDep,
    tenant_context: Annotated[TenantContext, Depends(require_scopes(ScopeName.TENANT_ADMIN))],
    _human_only: Annotated[TenantContext, Depends(require_human)],
) -> MembershipResponse:
    membership = await MembershipService(session).add_membership(
        user_id=body.user_id, role=body.role, tenant_context=tenant_context
    )
    await session.commit()
    return MembershipResponse.from_orm_membership(membership)
