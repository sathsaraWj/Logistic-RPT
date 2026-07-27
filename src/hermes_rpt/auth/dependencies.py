"""FastAPI dependencies for authentication and authorization.

The dependency chain is: bearer token -> verified `TokenClaims` -> trusted `TenantContext`.
Nothing here ever reads a tenant identifier from a header, query parameter, or request body —
`get_tenant_context` builds the `TenantContext` from `TokenClaims.tenant_id` alone (Phase 3
requirement 5). `require_scopes`/`require_roles`/`require_human`/`require_service` are
dependency *factories*: call them at route-declaration time with the requirement, and use the
returned callable as a `Depends(...)`.

Most of these do no I/O of their own (verification is in-process HS256, no network call) and
stay plain sync callables — `get_tenant_context` is the one exception, since it binds the
resolved tenant into the request's database session for Row-Level Security (see its docstring).

`require_scopes`/`require_roles`/`require_human`/`require_service` take the already-resolved
`TenantContext` as input, so they stay sync regardless.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_rpt.auth.claims import TokenClaims
from hermes_rpt.auth.enums import PrincipalType, ScopeName
from hermes_rpt.auth.errors import AuthenticationError, AuthorizationError
from hermes_rpt.auth.verifier import LocalDevTokenVerifier, TokenVerifier
from hermes_rpt.common.db import get_session
from hermes_rpt.common.settings import Settings, get_settings
from hermes_rpt.common.tenant_session import bind_tenant_for_row_level_security
from hermes_rpt.tenants.context import TenantContext

_bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def get_token_verifier() -> TokenVerifier:
    return LocalDevTokenVerifier(get_settings())


def get_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> str:
    if credentials is None:
        raise AuthenticationError("missing_token")
    return credentials.credentials


def get_token_claims(
    token: Annotated[str, Depends(get_bearer_token)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
) -> TokenClaims:
    return verifier.verify(token)


def _correlation_id_from_request(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


async def get_tenant_context(
    request: Request,
    claims: Annotated[TokenClaims, Depends(get_token_claims)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantContext:
    """The one place a `TenantContext` is built from a real request. `claims.tenant_id` is the
    *only* source of tenant identity here — any `X-Tenant-Id`-style header the client sent is
    never read (Phase 3 requirement 5).

    Also binds the tenant into this request's database session for PostgreSQL Row-Level
    Security (`bind_tenant_for_row_level_security` — a real, load-bearing call, not a formality:
    without it, every RLS-protected table's `FORCE ROW LEVEL SECURITY` policy sees
    `app.current_tenant_id` as unset and denies all access, including to the requesting
    tenant's own rows. `Depends(get_session)` is cached per-request, so this binds the exact
    same session instance every downstream repository call in this request will use."""

    tenant_context = TenantContext(
        tenant_id=claims.tenant_id,
        principal_id=claims.sub,
        roles=claims.roles,
        scopes=claims.scopes,
        correlation_id=_correlation_id_from_request(request),
        principal_type=claims.principal_type.value,
    )
    await bind_tenant_for_row_level_security(session, tenant_context)
    return tenant_context


TenantContextDep = Annotated[TenantContext, Depends(get_tenant_context)]


def require_scopes(*required: ScopeName) -> Callable[[TenantContextDep], TenantContext]:
    def _check(tenant_context: TenantContextDep) -> TenantContext:
        missing = {s.value for s in required} - tenant_context.scopes
        if missing:
            raise AuthorizationError(
                f"missing_scope:{','.join(sorted(missing))}",
                tenant_id=tenant_context.tenant_id,
                principal_id=tenant_context.principal_id,
            )
        return tenant_context

    return _check


def require_roles(*required: str) -> Callable[[TenantContextDep], TenantContext]:
    def _check(tenant_context: TenantContextDep) -> TenantContext:
        if not any(tenant_context.has_role(role) for role in required):
            raise AuthorizationError(
                f"missing_role:{','.join(sorted(required))}",
                tenant_id=tenant_context.tenant_id,
                principal_id=tenant_context.principal_id,
            )
        return tenant_context

    return _check


def require_human(tenant_context: TenantContextDep) -> TenantContext:
    """Rejects a service-account token on a route meant for a human principal — the concrete
    "service token used as a human token" check from Phase 3's security test list."""

    if tenant_context.principal_type != PrincipalType.HUMAN.value:
        raise AuthorizationError(
            "service_token_not_allowed_here",
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
        )
    return tenant_context


def require_service(tenant_context: TenantContextDep) -> TenantContext:
    if tenant_context.principal_type != PrincipalType.SERVICE.value:
        raise AuthorizationError(
            "human_token_not_allowed_here",
            tenant_id=tenant_context.tenant_id,
            principal_id=tenant_context.principal_id,
        )
    return tenant_context


def get_settings_dependency() -> Settings:
    return get_settings()
