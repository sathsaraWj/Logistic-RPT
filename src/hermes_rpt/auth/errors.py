"""Authentication/authorization errors.

Each error carries a `reason` code for internal logging/audit and a fixed, generic `detail`
that is what actually reaches the client — "return safe errors without leaking internal
details" (Phase 3 requirement 10). Never put claim values, token contents, or internal state
into `detail`.
"""

from __future__ import annotations

import uuid


class AuthenticationError(Exception):
    """The request could not be authenticated at all (maps to HTTP 401)."""

    detail = "Authentication required."

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AuthorizationError(Exception):
    """The request was authenticated but is not allowed to do this (maps to HTTP 403).

    Also used for the "tenant/resource mismatch" case — but see
    hermes_rpt.common.repository.TenantMismatchError, which callers should generally prefer to
    let surface as 404 instead, so as not to confirm a resource exists in another tenant.

    `tenant_id`/`principal_id` are optional and, when set by the raising dependency (which
    already resolved a `TenantContext`), let the audit-logging exception handler attribute the
    denial to the right tenant without re-parsing the token.
    """

    detail = "You do not have permission to perform this action."

    def __init__(
        self,
        reason: str,
        *,
        tenant_id: uuid.UUID | None = None,
        principal_id: uuid.UUID | None = None,
    ) -> None:
        self.reason = reason
        self.tenant_id = tenant_id
        self.principal_id = principal_id
        super().__init__(reason)
