"""`TokenClaims`: the validated shape of a Hermes-RPT access/service token, after signature,
issuer, audience and expiry have already been checked by a `TokenVerifier`.

This is deliberately a narrower, stricter model than "whatever the JWT library decoded" — a
missing or malformed `tenant_id`/`sub` claim fails Pydantic validation immediately rather than
propagating as a `None` that some downstream code might mishandle.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, field_validator

from hermes_rpt.auth.enums import PrincipalType


class TokenClaims(BaseModel):
    model_config = ConfigDict(frozen=True)

    sub: uuid.UUID  # principal_id
    tenant_id: uuid.UUID
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    principal_type: PrincipalType = PrincipalType.HUMAN
    iss: str
    aud: str
    exp: int
    iat: int
    jti: str

    @field_validator("roles", "scopes", mode="before")
    @classmethod
    def _coerce_iterable(cls, value: object) -> object:
        if isinstance(value, list | tuple | set | frozenset):
            return frozenset(value)
        return value
