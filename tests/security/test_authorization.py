"""Security tests for authorization: scopes, tenant/resource mismatch, cross-tenant object
access, and service-token-as-human-token (Phase 3 security test list, authorization half)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.auth.enums import PrincipalType
from hermes_rpt.tenants.context import TenantContext
from hermes_rpt.tenants.enums import RoleName
from hermes_rpt.tenants.service import MembershipService
from tests.security.conftest import AuthFixture


def test_missing_scope_is_rejected(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=[])  # no tenant:read
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 403
    assert response.json() == {"detail": "You do not have permission to perform this action."}


async def test_tenant_resource_mismatch_returns_404_not_403(
    auth_fixture: AuthFixture, session: AsyncSession
) -> None:
    """A membership that belongs to Tenant Beta must be invisible to a Tenant Alpha token —
    and indistinguishable from "doesn't exist," not "exists but you can't see it.\""""

    beta_context = TenantContext(
        tenant_id=auth_fixture.tenant_b.id, principal_id=auth_fixture.user_a.id
    )
    # Created directly via the service (bypassing the API) so this test is purely about the
    # read path's tenant isolation, not membership creation.
    beta_membership = await MembershipService(session).add_membership(
        user_id=auth_fixture.user_a.id, role=RoleName.OPERATOR, tenant_context=beta_context
    )
    await session.commit()

    alpha_token = auth_fixture.token_for(scopes=["tenant:read"])
    response = auth_fixture.client.get(
        f"/v1/memberships/{beta_membership.id}", headers=auth_fixture.auth_headers(alpha_token)
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found."}


def test_service_token_rejected_on_human_only_route(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=["tenant:admin"], principal_type=PrincipalType.SERVICE)
    response = auth_fixture.client.post(
        "/v1/memberships",
        json={"user_id": str(auth_fixture.user_a.id), "role": RoleName.OPERATOR.value},
        headers=auth_fixture.auth_headers(token),
    )
    assert response.status_code == 403


def test_human_token_allowed_on_human_only_route(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=["tenant:admin"])
    response = auth_fixture.client.post(
        "/v1/memberships",
        json={"user_id": str(auth_fixture.user_a.id), "role": RoleName.OPERATOR.value},
        headers=auth_fixture.auth_headers(token),
    )
    assert response.status_code == 201


def test_tenant_id_header_is_ignored(auth_fixture: AuthFixture) -> None:
    """Sending a header claiming to be Tenant Beta must not change which tenant the request is
    resolved as — the token's tenant_id claim is the only source (Phase 3 requirement 5)."""

    token = auth_fixture.token_for(scopes=["tenant:read"])  # token is for tenant_a
    headers = auth_fixture.auth_headers(token)
    headers["X-Tenant-Id"] = str(auth_fixture.tenant_b.id)

    response = auth_fixture.client.get("/v1/memberships", headers=headers)
    assert response.status_code == 200
    # Everything returned (if anything) must belong to tenant_a, never tenant_b, regardless of
    # the header.
    for membership in response.json():
        assert membership["tenant_id"] == str(auth_fixture.tenant_a.id)
