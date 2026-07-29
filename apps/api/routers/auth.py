"""External partner service-credential management and token exchange.

Every response model here is safe by construction with one deliberate exception:
`ServiceCredentialCreateResponse` carries the plaintext `client_secret` — the *only* place it is
ever returned, exactly once, at creation. `ServiceCredentialSummary` (used everywhere else,
including the create response's sibling list endpoint) never carries `client_secret` or
`secret_hash`. See docs/AUTHENTICATION.md §5a for the full design.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from apps.api.deps import DbSessionDep, ServiceCredentialServiceDep
from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.auth.dependencies import require_scopes
from hermes_rpt.auth.enums import ScopeName
from hermes_rpt.auth.models import ServiceCredential
from hermes_rpt.auth.rate_limit import InMemoryFixedWindowRateLimiter, RateLimitExceededError
from hermes_rpt.auth.service import ExpiryInThePastError, UngrantableScopeError
from hermes_rpt.auth.service_tokens import issue_service_token
from hermes_rpt.common.settings import get_settings
from hermes_rpt.tenants.context import TenantContext

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_ManageScope = Annotated[
    TenantContext, Depends(require_scopes(ScopeName.SERVICE_CREDENTIAL_MANAGE))
]

# Module-level singletons — same "process-wide, not multi-instance-safe" scope as every other
# use of ConnectionLifecycleManager's underlying pieces in apps/api/deps.py. The exchange
# endpoint is unauthenticated-by-Bearer-token-standards (the body *is* the credential), so it's
# the first real brute-force/enumeration target in the whole API — bounding retries per client_id
# and per source IP, not preventing online brute force of the secret itself (infeasible at its
# entropy regardless). See docs/AUTHENTICATION.md §5a for the numbers and the non-distributed
# caveat this inherits from hermes_rpt.auth.rate_limit's own module docstring.
_client_rate_limiter = InMemoryFixedWindowRateLimiter(max_requests=5, window_seconds=60)
_ip_rate_limiter = InMemoryFixedWindowRateLimiter(max_requests=20, window_seconds=60)


class ServiceCredentialCreateRequest(BaseModel):
    name: str
    scopes: list[str]
    expires_at: datetime | None = None


class ServiceCredentialCreateResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    client_id: str
    client_secret: str
    scopes: list[str]
    expires_at: datetime | None
    created_at: datetime


class ServiceCredentialSummary(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    client_id: str
    scopes: list[str]
    is_active: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_credential(cls, credential: ServiceCredential) -> ServiceCredentialSummary:
        return cls(
            id=credential.id,
            tenant_id=credential.tenant_id,
            name=credential.name,
            client_id=credential.client_id,
            scopes=credential.scopes,
            is_active=credential.is_active,
            expires_at=credential.expires_at,
            last_used_at=credential.last_used_at,
            revoked_at=credential.revoked_at,
            created_at=credential.created_at,
            updated_at=credential.updated_at,
        )


class ServiceTokenExchangeRequest(BaseModel):
    client_id: str
    client_secret: str


class ServiceTokenExchangeResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 # nosec B105 - OAuth2 field name, not a credential
    expires_in: int
    scope: str


@router.post(
    "/service-credentials",
    response_model=ServiceCredentialCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_service_credential(
    body: ServiceCredentialCreateRequest,
    service: ServiceCredentialServiceDep,
    session: DbSessionDep,
    tenant_context: _ManageScope,
) -> ServiceCredentialCreateResponse:
    try:
        credential, client_secret = await service.issue_credential(
            tenant_context=tenant_context,
            name=body.name,
            scopes=body.scopes,
            expires_at=body.expires_at,
        )
    except UngrantableScopeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ExpiryInThePastError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await session.commit()
    return ServiceCredentialCreateResponse(
        id=credential.id,
        tenant_id=credential.tenant_id,
        name=credential.name,
        client_id=credential.client_id,
        client_secret=client_secret,
        scopes=credential.scopes,
        expires_at=credential.expires_at,
        created_at=credential.created_at,
    )


@router.get("/service-credentials", response_model=list[ServiceCredentialSummary])
async def list_service_credentials(
    service: ServiceCredentialServiceDep, tenant_context: _ManageScope
) -> list[ServiceCredentialSummary]:
    credentials = await service.list_for_tenant(tenant_context=tenant_context)
    return [ServiceCredentialSummary.from_orm_credential(c) for c in credentials]


@router.delete("/service-credentials/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_service_credential(
    credential_id: uuid.UUID,
    service: ServiceCredentialServiceDep,
    session: DbSessionDep,
    tenant_context: _ManageScope,
) -> None:
    await service.revoke(credential_id, tenant_context=tenant_context)
    await session.commit()


@router.post("/service-token", response_model=ServiceTokenExchangeResponse)
async def exchange_service_token(
    body: ServiceTokenExchangeRequest,
    request: Request,
    service: ServiceCredentialServiceDep,
    session: DbSessionDep,
) -> ServiceTokenExchangeResponse:
    # No Depends(require_scopes(...)) here — this is the one endpoint in the API whose body
    # *is* the authentication. See module docstring and docs/AUTHENTICATION.md §5a.
    client_ip = request.client.host if request.client else "unknown"
    try:
        _client_rate_limiter.check(f"client:{body.client_id}")
        _ip_rate_limiter.check(f"ip:{client_ip}")
    except RateLimitExceededError as exc:
        await AuditService(session).record(
            action="auth.service_token_exchange",
            outcome=AuditOutcome.DENIED,
            principal_type="service_credential",
            details={"reason": "rate_limited"},
        )
        await session.commit()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests.") from exc

    credential = await service.authenticate(
        client_id=body.client_id, client_secret=body.client_secret
    )
    settings = get_settings()
    access_token = issue_service_token(
        settings=settings,
        tenant_id=credential.tenant_id,
        principal_id=credential.id,
        scopes=credential.scopes,
    )
    await session.commit()
    return ServiceTokenExchangeResponse(
        access_token=access_token,
        expires_in=settings.service_token_ttl_seconds,
        scope=" ".join(credential.scopes),
    )
