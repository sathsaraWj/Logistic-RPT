"""`TenantContext`: the one trusted source of "who is asking, for which tenant" that every
tenant-owned repository/service method requires.

**This module never resolves a TenantContext from client input.** Construction from a real
request happens only in Phase 3 (`hermes_rpt.auth`), from verified JWT claims or a trusted
service identity — never from a query parameter, request body field, or client-supplied
header. Code outside `hermes_rpt.auth` (tests, factories, Phase 2 service-layer examples) may
construct a `TenantContext` directly, but that is explicitly a "trusted caller already decided
this" operation, not a re-implementation of authentication.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: uuid.UUID
    principal_id: uuid.UUID
    roles: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)
    correlation_id: str | None = None
    # "human" or "service" — set from the verified token's principal_type claim (Phase 3).
    # Lets route-level checks reject a service credential used where a human is required, or
    # vice versa, without re-deriving that from roles/scopes.
    principal_type: str = "human"

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def is_service_account(self) -> bool:
        return self.principal_type == "service"
