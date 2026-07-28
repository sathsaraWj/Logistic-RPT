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
from hermes_rpt.common.tenant_session import bind_tenant_for_row_level_security
from hermes_rpt.monitoring import metrics

logger = get_logger(__name__)


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


async def _record_audit_event_best_effort(request: Request, /, **kwargs: object) -> None:
    session_factory = getattr(request.app.state, "db_sessionmaker", None) or get_sessionmaker()
    try:
        async with session_factory() as session:
            # A Phase 16 review found this session never bound `app.current_tenant_id` at all —
            # combined with a second bug in the audit_events RLS policy itself (fixed in
            # migration b48a21a99ef0: `NULL = NULL` is not `TRUE` in SQL, so a platform-level,
            # tenant_id-IS-NULL event — e.g. every authentication failure — could never be
            # inserted under FORCE ROW LEVEL SECURITY, silently, since this write is
            # best-effort). `kwargs.get("tenant_id")` is `None` for the auth-failure case, which
            # `bind_tenant_for_row_level_security` treats as "leave unbound" — correct now that
            # the policy actually admits a NULL-tenant row from an unbound session.
            await bind_tenant_for_row_level_security(session, kwargs.get("tenant_id"))  # type: ignore[arg-type]
            await AuditService(session).record(**kwargs)  # type: ignore[arg-type]
            await session.commit()
    except Exception:  # noqa: BLE001 - audit-write failure must not block the auth response
        logger.error("audit_event_write_failed", action=kwargs.get("action"))


async def authentication_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthenticationError)  # nosec B101 - narrows type; FastAPI-guaranteed
    logger.warning("authentication_failed", reason=exc.reason, path=request.url.path)
    metrics.authentication_failures_total.labels(reason=exc.reason).inc()

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
    metrics.authorization_failures_total.labels(
        tenant_id=str(exc.tenant_id) if exc.tenant_id else "unknown", reason=exc.reason
    ).inc()

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


async def tenant_mismatch_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, TenantMismatchError)  # nosec B101 - narrows type; FastAPI-guaranteed
    # Deliberately 404, not 403 — see TenantMismatchError docstring: the platform must not
    # confirm to a caller that a resource belonging to another tenant exists.
    logger.info("tenant_resource_mismatch", path=request.url.path)

    # request.state.tenant_id is the *caller's own* tenant (set by get_tenant_context once auth
    # succeeded) — never the tenant the almost-reached resource actually belongs to, which this
    # handler must not reveal even internally in a metric label.
    caller_tenant_id = getattr(request.state, "tenant_id", None)
    metrics.cross_tenant_access_attempts_total.labels(
        tenant_id=str(caller_tenant_id) if caller_tenant_id else "unknown"
    ).inc()

    await _record_audit_event_best_effort(
        request,
        action="tenant.cross_tenant_access_attempt",
        outcome=AuditOutcome.DENIED,
        tenant_id=caller_tenant_id,
        correlation_id=_correlation_id(request),
        details={"path": request.url.path},
    )

    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "Not found."},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuthenticationError, authentication_error_handler)
    app.add_exception_handler(AuthorizationError, authorization_error_handler)
    app.add_exception_handler(TenantMismatchError, tenant_mismatch_handler)
