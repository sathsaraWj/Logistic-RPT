"""Maps authentication/authorization/tenant-isolation exceptions to safe HTTP responses, and
records an `AuditEvent` for every one of them (Phase 3 requirement 8).

Registered once in `apps.api.main.create_app`. Each handler opens its own short-lived
control-plane session via `request.app.state.db_sessionmaker` (set to
`hermes_rpt.common.db.get_sessionmaker()` by default, and overridable — e.g. by tests, which
point it at an in-memory database instead) rather than calling `get_sessionmaker()` directly,
since exception handlers sit outside FastAPI's `Depends()` machinery and so cannot use
`app.dependency_overrides`.

The audit write is best-effort: if it fails (e.g. the control-plane database is briefly
unreachable), the handler still returns the correct 401/403 response rather than turning an
auth failure into an unrelated 500 — it logs the audit-write failure instead so that failure is
itself visible, just not blocking.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from hermes_rpt.audit.enums import AuditOutcome
from hermes_rpt.audit.service import AuditService
from hermes_rpt.auth.errors import AuthenticationError, AuthorizationError
from hermes_rpt.common.db import get_sessionmaker
from hermes_rpt.common.logging import get_logger
from hermes_rpt.common.repository import TenantMismatchError

logger = get_logger(__name__)


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


async def _record_audit_event_best_effort(request: Request, /, **kwargs: object) -> None:
    session_factory = getattr(request.app.state, "db_sessionmaker", None) or get_sessionmaker()
    try:
        async with session_factory() as session:
            await AuditService(session).record(**kwargs)  # type: ignore[arg-type]
            await session.commit()
    except Exception:  # noqa: BLE001 - audit-write failure must not block the auth response
        logger.error("audit_event_write_failed", action=kwargs.get("action"))


async def authentication_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthenticationError)  # nosec B101 - narrows type; FastAPI-guaranteed
    logger.warning("authentication_failed", reason=exc.reason, path=request.url.path)

    await _record_audit_event_best_effort(
        request,
        action="auth.authenticate",
        outcome=AuditOutcome.DENIED,
        principal_type="unknown",
        correlation_id=_correlation_id(request),
        details={"reason": exc.reason, "path": request.url.path},
    )

    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": exc.detail},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authorization_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthorizationError)  # nosec B101 - narrows type; FastAPI-guaranteed
    logger.warning(
        "authorization_denied",
        reason=exc.reason,
        path=request.url.path,
        tenant_id=str(exc.tenant_id) if exc.tenant_id else None,
    )

    await _record_audit_event_best_effort(
        request,
        action="authz.check",
        outcome=AuditOutcome.DENIED,
        tenant_id=exc.tenant_id,
        principal_id=exc.principal_id,
        correlation_id=_correlation_id(request),
        details={"reason": exc.reason, "path": request.url.path},
    )

    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": exc.detail},
    )


def tenant_mismatch_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, TenantMismatchError)  # nosec B101 - narrows type; FastAPI-guaranteed
    # Deliberately 404, not 403 — see TenantMismatchError docstring: the platform must not
    # confirm to a caller that a resource belonging to another tenant exists.
    logger.info("tenant_resource_mismatch", path=request.url.path)
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "Not found."},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuthenticationError, authentication_error_handler)
    app.add_exception_handler(AuthorizationError, authorization_error_handler)
    app.add_exception_handler(TenantMismatchError, tenant_mismatch_handler)
