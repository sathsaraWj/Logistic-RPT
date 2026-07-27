"""Security tests for token verification (Phase 3 security test list, authentication half)."""

from __future__ import annotations

import jwt

from hermes_rpt.common.settings import get_settings
from tests.security.conftest import AuthFixture


def test_missing_token_is_rejected(auth_fixture: AuthFixture) -> None:
    response = auth_fixture.client.get("/v1/memberships")
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_garbage_token_is_rejected(auth_fixture: AuthFixture) -> None:
    response = auth_fixture.client.get(
        "/v1/memberships", headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert response.status_code == 401


def test_expired_token_is_rejected(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=["tenant:read"], ttl_seconds=-3600)
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 401


def test_invalid_issuer_is_rejected(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=["tenant:read"], issuer="https://not-hermes.example")
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 401


def test_invalid_audience_is_rejected(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=["tenant:read"], audience="some-other-api")
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 401


def test_missing_tenant_claim_is_rejected(auth_fixture: AuthFixture) -> None:
    settings = get_settings()
    # Bypass issue_dev_token deliberately — it always sets tenant_id — to prove the *verifier*
    # itself, not just the helper, requires the claim.
    payload = {
        "sub": str(auth_fixture.user_a.id),
        "roles": [],
        "scopes": ["tenant:read"],
        "principal_type": "human",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": 0,
        "exp": 99999999999,
        "jti": "test-missing-tenant",
    }
    token = jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 401


def test_wrong_signing_secret_is_rejected(auth_fixture: AuthFixture) -> None:
    settings = get_settings()
    payload = {
        "sub": str(auth_fixture.user_a.id),
        "tenant_id": str(auth_fixture.tenant_a.id),
        "roles": [],
        "scopes": ["tenant:read"],
        "principal_type": "human",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": 0,
        "exp": 99999999999,
        "jti": "test-wrong-secret",
    }
    token = jwt.encode(payload, "totally-different-secret", algorithm=settings.jwt_algorithm)
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 401


def test_authentication_failure_does_not_leak_internal_detail(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=["tenant:read"], ttl_seconds=-3600)
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))
    body = response.json()
    assert body == {"detail": "Authentication required."}
    assert "expired" not in response.text.lower()
    assert "signature" not in response.text.lower()


def test_valid_token_is_accepted(auth_fixture: AuthFixture) -> None:
    token = auth_fixture.token_for(scopes=["tenant:read"])
    response = auth_fixture.client.get("/v1/memberships", headers=auth_fixture.auth_headers(token))
    assert response.status_code == 200
