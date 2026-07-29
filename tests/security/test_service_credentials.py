"""Adversarial coverage for external partner service credentials (docs/AUTHENTICATION.md §5a).

Mirrors tests/security/test_authentication.py / test_authorization.py in style: exercised
through the real HTTP API via `auth_fixture`, one behaviour per test, generic-detail assertions
where the platform is required to not leak internal state.
"""

from __future__ import annotations

import uuid
from typing import cast

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.audit.models import AuditEvent
from hermes_rpt.audit.repository import AuditEventRepository
from hermes_rpt.auth.enums import PrincipalType, ScopeName
from hermes_rpt.auth.verifier import LocalDevTokenVerifier
from hermes_rpt.common.settings import get_settings
from hermes_rpt.tenants.context import TenantContext
from tests.security.conftest import AuthFixture

pytestmark = pytest.mark.asyncio


def _manage_headers(auth_fixture: AuthFixture) -> dict[str, str]:
    token = auth_fixture.token_for(scopes=[ScopeName.SERVICE_CREDENTIAL_MANAGE.value])
    return auth_fixture.auth_headers(token)


def _create_credential(
    auth_fixture: AuthFixture, *, scopes: list[str] | None = None
) -> dict[str, object]:
    response = auth_fixture.client.post(
        "/v1/auth/service-credentials",
        json={
            "name": "hermes-vms-integration",
            "scopes": scopes or [ScopeName.PREDICTION_EXECUTE.value],
        },
        headers=_manage_headers(auth_fixture),
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


async def test_creating_a_credential_requires_the_manage_scope(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=[])
    response = auth_fixture.client.post(
        "/v1/auth/service-credentials",
        json={"name": "x", "scopes": []},
        headers=auth_fixture.auth_headers(token),
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "You do not have permission to perform this action."}


async def test_listing_credentials_requires_the_manage_scope(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=[])
    response = auth_fixture.client.get(
        "/v1/auth/service-credentials", headers=auth_fixture.auth_headers(token)
    )
    assert response.status_code == 403


async def test_revoking_a_credential_requires_the_manage_scope(auth_fixture: AuthFixture) -> None:
    created = _create_credential(auth_fixture)
    token = auth_fixture.token_for(scopes=[])
    response = auth_fixture.client.delete(
        f"/v1/auth/service-credentials/{created['id']}", headers=auth_fixture.auth_headers(token)
    )
    assert response.status_code == 403


async def test_creation_rejects_tenant_admin_scope(auth_fixture: AuthFixture) -> None:
    response = auth_fixture.client.post(
        "/v1/auth/service-credentials",
        json={"name": "x", "scopes": [ScopeName.TENANT_ADMIN.value]},
        headers=_manage_headers(auth_fixture),
    )
    assert response.status_code == 400


async def test_creation_rejects_service_credential_manage_scope(auth_fixture: AuthFixture) -> None:
    response = auth_fixture.client.post(
        "/v1/auth/service-credentials",
        json={"name": "x", "scopes": [ScopeName.SERVICE_CREDENTIAL_MANAGE.value]},
        headers=_manage_headers(auth_fixture),
    )
    assert response.status_code == 400


async def test_creation_rejects_an_expiry_already_in_the_past(auth_fixture: AuthFixture) -> None:
    response = auth_fixture.client.post(
        "/v1/auth/service-credentials",
        json={
            "name": "x",
            "scopes": [ScopeName.PREDICTION_EXECUTE.value],
            "expires_at": "2000-01-01T00:00:00Z",
        },
        headers=_manage_headers(auth_fixture),
    )
    assert response.status_code == 400


async def test_create_response_contains_the_plaintext_secret_exactly_once(
    auth_fixture: AuthFixture,
) -> None:
    created = _create_credential(auth_fixture)
    assert "client_secret" in created
    assert isinstance(created["client_secret"], str)
    assert len(created["client_secret"]) > 20


async def test_list_response_never_contains_the_secret_or_a_hash(auth_fixture: AuthFixture) -> None:
    _create_credential(auth_fixture)
    response = auth_fixture.client.get(
        "/v1/auth/service-credentials", headers=_manage_headers(auth_fixture)
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 1
    for row in body:
        assert "client_secret" not in row
        assert "secret_hash" not in row


async def test_correct_exchange_returns_a_working_service_token(auth_fixture: AuthFixture) -> None:
    created = _create_credential(auth_fixture, scopes=[ScopeName.PREDICTION_EXECUTE.value])

    exchange = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": created["client_secret"]},
    )
    assert exchange.status_code == 200, exchange.text
    body = exchange.json()
    assert body["token_type"] == "bearer"
    assert body["scope"] == ScopeName.PREDICTION_EXECUTE.value

    claims = LocalDevTokenVerifier(get_settings()).verify(body["access_token"])
    assert claims.principal_type == PrincipalType.SERVICE
    assert claims.tenant_id == uuid.UUID(cast(str, created["tenant_id"]))
    assert claims.scopes == frozenset([ScopeName.PREDICTION_EXECUTE.value])


async def test_exchange_with_wrong_secret_is_rejected(auth_fixture: AuthFixture) -> None:
    created = _create_credential(auth_fixture)
    response = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": "not-the-real-secret"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}


async def test_exchange_with_unknown_client_id_is_rejected_identically(
    auth_fixture: AuthFixture,
) -> None:
    response = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": "hrpt_svc_does-not-exist", "client_secret": "whatever"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required."}


async def test_revoked_credential_cannot_be_exchanged(auth_fixture: AuthFixture) -> None:
    created = _create_credential(auth_fixture)
    revoke = auth_fixture.client.delete(
        f"/v1/auth/service-credentials/{created['id']}", headers=_manage_headers(auth_fixture)
    )
    assert revoke.status_code == 204

    exchange = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": created["client_secret"]},
    )
    assert exchange.status_code == 401


async def test_a_token_issued_before_revocation_still_verifies_until_its_own_expiry(
    auth_fixture: AuthFixture,
) -> None:
    """Documents the design's explicit tradeoff (docs/AUTHENTICATION.md §5a): revocation stops
    *future* exchanges immediately, but does not retroactively invalidate a token already
    handed out — that's the bounded blast radius the whole feature trades for not needing a
    jti denylist. This is the expected behaviour, not a bug."""

    created = _create_credential(auth_fixture)
    exchange = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": created["client_secret"]},
    )
    assert exchange.status_code == 200
    access_token = exchange.json()["access_token"]

    revoke = auth_fixture.client.delete(
        f"/v1/auth/service-credentials/{created['id']}", headers=_manage_headers(auth_fixture)
    )
    assert revoke.status_code == 204

    # The already-issued token is unaffected by revocation — verifies fine until its own `exp`.
    claims = LocalDevTokenVerifier(get_settings()).verify(access_token)
    assert claims.principal_type == PrincipalType.SERVICE


async def test_expired_credential_cannot_be_exchanged(
    auth_fixture: AuthFixture, session: AsyncSession
) -> None:
    from datetime import UTC, datetime, timedelta

    from hermes_rpt.auth.models import ServiceCredential

    created = _create_credential(auth_fixture)
    row = await session.get(ServiceCredential, uuid.UUID(cast(str, created["id"])))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    exchange = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": created["client_secret"]},
    )
    assert exchange.status_code == 401


async def test_a_credential_cannot_yield_a_token_for_another_tenant(
    auth_fixture: AuthFixture,
) -> None:
    created = _create_credential(auth_fixture)
    exchange = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": created["client_secret"]},
    )
    assert exchange.status_code == 200
    claims = LocalDevTokenVerifier(get_settings()).verify(exchange.json()["access_token"])
    assert claims.tenant_id == auth_fixture.tenant_a.id
    assert claims.tenant_id != auth_fixture.tenant_b.id


async def test_tenant_bs_admin_never_sees_tenant_as_credential(auth_fixture: AuthFixture) -> None:
    _create_credential(auth_fixture)  # created under tenant A

    token_b = auth_fixture.token_for(
        tenant_id=auth_fixture.tenant_b.id, scopes=[ScopeName.SERVICE_CREDENTIAL_MANAGE.value]
    )
    response = auth_fixture.client.get(
        "/v1/auth/service-credentials", headers=auth_fixture.auth_headers(token_b)
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_revoking_another_tenants_credential_behaves_like_not_found(
    auth_fixture: AuthFixture,
) -> None:
    created = _create_credential(auth_fixture)  # tenant A

    token_b = auth_fixture.token_for(
        tenant_id=auth_fixture.tenant_b.id, scopes=[ScopeName.SERVICE_CREDENTIAL_MANAGE.value]
    )
    response = auth_fixture.client.delete(
        f"/v1/auth/service-credentials/{created['id']}", headers=auth_fixture.auth_headers(token_b)
    )
    assert response.status_code == 404


async def test_rate_limit_trips_after_repeated_failed_exchanges(auth_fixture: AuthFixture) -> None:
    client_id = f"hrpt_svc_rate-limit-probe-{uuid.uuid4().hex[:8]}"
    statuses = []
    for _ in range(6):
        response = auth_fixture.client.post(
            "/v1/auth/service-token",
            json={"client_id": client_id, "client_secret": "wrong"},
        )
        statuses.append(response.status_code)
    assert statuses[:5] == [401, 401, 401, 401, 401]
    assert statuses[5] == 429


async def test_a_forged_alg_none_token_shaped_like_this_flows_output_is_still_rejected(
    auth_fixture: AuthFixture,
) -> None:
    """The exchange flow issues tokens through the exact same `build_signed_token`/
    `LocalDevTokenVerifier` machinery every other token uses — no new trust root. This closes a
    gap the existing suite doesn't cover anywhere: an attacker who intercepts a service-token
    exchange response and tries to re-sign a lookalike payload with `alg=none` must still fail."""

    created = _create_credential(auth_fixture)
    exchange = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": created["client_secret"]},
    )
    assert exchange.status_code == 200
    real_claims = jwt.decode(exchange.json()["access_token"], options={"verify_signature": False})

    forged = jwt.encode(real_claims, key="", algorithm="none")
    with pytest.raises(Exception):  # noqa: B017 - PyJWT/verifier internals raise different types
        LocalDevTokenVerifier(get_settings()).verify(forged)


async def test_audit_events_are_recorded_for_create_revoke_and_exchange(
    auth_fixture: AuthFixture, session: AsyncSession
) -> None:
    created = _create_credential(auth_fixture)

    ok = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": created["client_secret"]},
    )
    assert ok.status_code == 200

    bad = auth_fixture.client.post(
        "/v1/auth/service-token",
        json={"client_id": created["client_id"], "client_secret": "wrong"},
    )
    assert bad.status_code == 401

    revoke = auth_fixture.client.delete(
        f"/v1/auth/service-credentials/{created['id']}", headers=_manage_headers(auth_fixture)
    )
    assert revoke.status_code == 204

    tenant_events = await AuditEventRepository(session).list_for_tenant(
        tenant_context=_tenant_context_for(auth_fixture)
    )
    tenant_actions = {e.action for e in tenant_events}
    assert "service_credential.create" in tenant_actions
    assert "service_credential.revoke" in tenant_actions

    exchange_events = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "auth.service_token_exchange")
            )
        )
        .scalars()
        .all()
    )
    outcomes = {e.outcome for e in exchange_events}
    assert "success" in outcomes
    assert "denied" in outcomes


def _tenant_context_for(auth_fixture: AuthFixture) -> TenantContext:
    return TenantContext(tenant_id=auth_fixture.tenant_a.id, principal_id=auth_fixture.user_a.id)
