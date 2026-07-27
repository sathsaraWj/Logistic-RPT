"""Signed test/dev token issuance.

There is no real login flow in this repository yet (Phase 3 builds the verification side of
auth; a real OIDC provider is integrated later — see docs/adr/0010-jwt-local-dev-provider.md).
This module exists so local development and the test suite have a way to produce tokens that
`LocalDevTokenVerifier` will accept, without a production credential ever passing through it.

**Refuses to run outside local/CI environments** — this is signing tokens with the same shared
secret the verifier checks against, which is only ever an acceptable arrangement in
development.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

import jwt

from hermes_rpt.auth.enums import PrincipalType
from hermes_rpt.auth.verifier import new_jti
from hermes_rpt.common.settings import Environment, Settings


class DevTokenIssuanceNotAllowedError(Exception):
    pass


def _require_dev_environment(settings: Settings) -> None:
    if settings.environment not in (Environment.LOCAL, Environment.CI):
        raise DevTokenIssuanceNotAllowedError(
            f"Refusing to issue a dev-signed token in environment={settings.environment!r}"
        )


def issue_dev_token(
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    roles: Iterable[str] = (),
    scopes: Iterable[str] = (),
    principal_type: PrincipalType = PrincipalType.HUMAN,
    ttl_seconds: int | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    now: int | None = None,
) -> str:
    """Issues a signed token for local development / tests. `ttl_seconds` defaults to the
    principal-type-appropriate short-lived TTL from settings (Phase 3 requirement 7)."""

    _require_dev_environment(settings)

    issued_at = now if now is not None else int(time.time())
    default_ttl = (
        settings.service_token_ttl_seconds
        if principal_type == PrincipalType.SERVICE
        else settings.access_token_ttl_seconds
    )
    expires_at = issued_at + (ttl_seconds if ttl_seconds is not None else default_ttl)

    payload = {
        "sub": str(principal_id),
        "tenant_id": str(tenant_id),
        "roles": list(roles),
        "scopes": list(scopes),
        "principal_type": principal_type.value,
        "iss": issuer or settings.jwt_issuer,
        "aud": audience or settings.jwt_audience,
        "iat": issued_at,
        "exp": expires_at,
        "jti": new_jti(),
    }
    return jwt.encode(
        payload, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
