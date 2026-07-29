"""Shared JWT-building/signing logic, extracted from `hermes_rpt.auth.dev_tokens` so it can be
reused by a second, non-dev-gated caller (`hermes_rpt.auth.service_tokens`) without duplicating
the payload shape or touching `LocalDevTokenVerifier`'s trust root.

`build_signed_token` itself enforces no authorization policy at all — it will happily sign
whatever claims it's given with the platform's global `jwt_secret_key`. Every caller is
responsible for its own gate before calling this: `issue_dev_token` requires
`environment in (local, ci)`; `issue_service_token` requires an already-authenticated
`ServiceCredential`. Do not call this directly from a route.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

import jwt

from hermes_rpt.auth.enums import PrincipalType
from hermes_rpt.auth.verifier import new_jti
from hermes_rpt.common.settings import Settings


def build_signed_token(
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    roles: Iterable[str] = (),
    scopes: Iterable[str] = (),
    principal_type: PrincipalType,
    ttl_seconds: int | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    now: int | None = None,
) -> str:
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
