"""Correlation ID / request ID propagation.

Every inbound HTTP request gets a request ID (always generated server-side) and a
correlation ID (reused from an inbound header if the caller supplied one, so a chain of
internal service calls can be traced end to end; otherwise generated). Both are bound into
structlog's contextvars so every log line emitted while handling the request carries them
without call sites having to pass them explicitly, and both are echoed back as response
headers.

Note: the correlation ID is a tracing aid, not a trust boundary. It must never be used to
resolve tenant identity or authorization — see docs/adr/0003-tenant-isolation-defense-in-depth.md
and TenantContext (added in Phase 3), which is resolved only from verified auth claims.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_MAX_HEADER_LEN = 128


def _sanitize_incoming_id(raw: str | None) -> str | None:
    if not raw:
        return None
    candidate = raw.strip()[:_MAX_HEADER_LEN]
    return candidate or None


class CorrelationIdMiddleware:
    """Pure-ASGI middleware (no BaseHTTPMiddleware) binding request/correlation IDs."""

    def __init__(
        self,
        app: ASGIApp,
        request_id_header: str = "X-Request-ID",
        correlation_id_header: str = "X-Correlation-ID",
    ) -> None:
        self.app = app
        self.request_id_header = request_id_header
        self.correlation_id_header = correlation_id_header

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = str(uuid.uuid4())
        correlation_id = (
            _sanitize_incoming_id(request.headers.get(self.correlation_id_header)) or request_id
        )
        # Available to route dependencies (e.g. hermes_rpt.auth.dependencies) via
        # `request.state.correlation_id`, so audit events for auth failures can be linked back
        # to the same correlation ID this request's log lines and response headers carry.
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((self.request_id_header.lower().encode(), request_id.encode()))
                headers.append(
                    (self.correlation_id_header.lower().encode(), correlation_id.encode())
                )
            await send(message)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, correlation_id=correlation_id)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.clear_contextvars()


HandlerFn = Callable[[Request], Awaitable[Response]]
