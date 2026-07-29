"""Issues a short-lived `principal_type=SERVICE` token for an already-authenticated external
partner (`hermes_rpt.auth.service.ServiceCredentialService.authenticate`). Unlike
`hermes_rpt.auth.dev_tokens.issue_dev_token`, this has no environment gate — its authorization
gate is upstream: only `ServiceCredentialService.authenticate` calls this, and only after it has
verified a valid, active, non-expired `ServiceCredential`. Never call this directly from a
route."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from hermes_rpt.auth.enums import PrincipalType
from hermes_rpt.auth.token_issuance import build_signed_token
from hermes_rpt.common.settings import Settings


def issue_service_token(
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    scopes: Iterable[str],
    now: int | None = None,
) -> str:
    """A service credential has no role concept (`roles=()`) — only the fixed `scopes` it was
    created with. TTL always comes from `settings.service_token_ttl_seconds` (never
    caller-overridden — an external partner cannot request a longer-lived token than the
    platform allows)."""

    return build_signed_token(
        settings=settings,
        tenant_id=tenant_id,
        principal_id=principal_id,
        roles=(),
        scopes=scopes,
        principal_type=PrincipalType.SERVICE,
        ttl_seconds=settings.service_token_ttl_seconds,
        now=now,
    )
