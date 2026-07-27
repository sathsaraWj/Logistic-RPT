"""Token verification.

`TokenVerifier` is the seam between "however tokens are actually signed" and everything else in
the platform, which only ever deals in verified `TokenClaims`. `LocalDevTokenVerifier` is an
HS256/shared-secret implementation suitable for local development and CI only; it validates
signature, issuer, audience, expiry and required claims — nothing more, nothing custom (no
hand-rolled crypto, per Phase 3 requirement 11 — this is PyJWT end to end).

A production deployment is expected to implement this same `TokenVerifier` protocol against a
real OpenID Connect provider's JWKS endpoint (RS256, rotating keys, no shared secret) — that
adapter is not built in this repository yet (see docs/adr/0010-jwt-local-dev-provider.md and
TASKS.md), exactly the same "interface now, cloud adapter later" shape as
hermes_rpt.secrets.SecretProvider (ADR-0005).
"""

from __future__ import annotations

import uuid
from typing import Protocol

import jwt
from pydantic import ValidationError

from hermes_rpt.auth.claims import TokenClaims
from hermes_rpt.auth.errors import AuthenticationError
from hermes_rpt.common.settings import Settings


class TokenVerifier(Protocol):
    def verify(self, token: str) -> TokenClaims: ...


class LocalDevTokenVerifier:
    """HS256 shared-secret verifier. Never used with `jwt_secret_key` left at its insecure
    default outside local/CI — `Settings` refuses to start that way (fail closed), so reaching
    this code at all in staging/production implies a real secret is configured."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def verify(self, token: str) -> TokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._settings.jwt_secret_key.get_secret_value(),
                algorithms=[self._settings.jwt_algorithm],
                issuer=self._settings.jwt_issuer,
                audience=self._settings.jwt_audience,
                leeway=self._settings.jwt_leeway_seconds,
                options={"require": ["exp", "iat", "sub", "tenant_id", "iss", "aud", "jti"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("expired_token") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthenticationError("invalid_issuer") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthenticationError("invalid_audience") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthenticationError("missing_required_claim") from exc
        except jwt.InvalidSignatureError as exc:
            raise AuthenticationError("invalid_signature") from exc
        except jwt.PyJWTError as exc:
            # Catch-all for anything else PyJWT considers malformed (bad base64, wrong number
            # of segments, unsupported algorithm, ...) — still just "the token is bad."
            raise AuthenticationError("malformed_token") from exc

        try:
            return TokenClaims.model_validate(payload)
        except ValidationError as exc:
            raise AuthenticationError("invalid_claim_shape") from exc


def new_jti() -> str:
    return uuid.uuid4().hex
