# Authentication and Authorization

Status: implements Phase 3 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). See
[THREAT_MODEL.md](THREAT_MODEL.md) §5 (spoofing/elevation-of-privilege threats) and
[adr/0010-jwt-local-dev-provider.md](adr/0010-jwt-local-dev-provider.md) for the design
rationale.

## 1. Flow

```text
Client
  │  Authorization: Bearer <JWT>
  ▼
HTTPBearer (FastAPI security scheme)          hermes_rpt.auth.dependencies.get_bearer_token
  │  missing token -> AuthenticationError("missing_token")
  ▼
TokenVerifier.verify(token)                   hermes_rpt.auth.verifier.LocalDevTokenVerifier
  │  checks signature, issuer, audience, expiry, required claims (PyJWT; no custom crypto)
  │  any failure -> AuthenticationError(reason)   [never leaks *why* to the client]
  ▼
TokenClaims (validated Pydantic model)        hermes_rpt.auth.claims.TokenClaims
  │
  ▼
TenantContext                                 hermes_rpt.auth.dependencies.get_tenant_context
  │  built ONLY from TokenClaims.tenant_id — an X-Tenant-Id-style header is never read
  ▼
require_scopes(...) / require_roles(...) / require_human / require_service
  │  missing requirement -> AuthorizationError(reason, tenant_id, principal_id)
  ▼
Route handler → repository/service layer (hermes_rpt.common.repository.TenantScopedRepository)
  │  wrong-tenant resource id -> TenantMismatchError -> mapped to 404, not 403
  ▼
Response
```

Every `AuthenticationError` and `AuthorizationError` is caught by handlers registered in
`apps/api/exception_handlers.py`, which (a) return a fixed, generic `detail` message — never
the specific `reason` code, a claim value, or an internal exception message — and (b) write an
`AuditEvent` (Phase 3 requirement 8) before responding.

## 2. Trust boundaries

* **Client → API**: untrusted. The only thing trusted from the client is the bearer token
  itself, and only after full verification. No header, query parameter, or request body field
  is ever used to select a tenant, a role, or a principal — see
  `hermes_rpt.auth.dependencies.get_tenant_context` docstring (Phase 3 requirement 5).
* **API → TokenVerifier**: trusted once `verify()` returns without raising. Everything after
  that point treats `TokenClaims` as ground truth for who is asking and for which tenant.
* **API → repository/service layer**: the repository layer re-derives tenant ownership from
  the database row itself and compares it against the trusted `TenantContext`
  (`docs/adr/0003-tenant-isolation-defense-in-depth.md`) — authentication establishes *who*,
  the repository layer independently enforces *what they can touch*.
* **Correlation ID**: propagated for tracing/audit linkage only. It is never used to resolve
  identity or authorization, and is explicitly documented as such in
  `hermes_rpt.common.correlation`.

## 3. Token shape

| Claim | Meaning |
|---|---|
| `sub` | Principal ID (UUID) — a `User.id` for human tokens, a service identity's UUID for service tokens |
| `tenant_id` | The **only** source of tenant identity used anywhere in a request |
| `roles` | `RoleName` values the principal holds in that tenant |
| `scopes` | `ScopeName` values this specific token is allowed to use |
| `principal_type` | `"human"` or `"service"` — see `PrincipalType` |
| `iss`, `aud` | Checked against `Settings.jwt_issuer` / `Settings.jwt_audience` |
| `exp`, `iat` | Standard expiry/issued-at; `Settings.jwt_leeway_seconds` tolerates clock skew |
| `jti` | Unique token ID (unused for revocation yet — see TASKS.md) |

## 4. Scopes

`tenant:read`, `tenant:admin`, `connection:manage`, `schema:discover`, `mapping:manage`,
`prediction:execute`, `model:train`, `model:promote`, `audit:read` — see
`hermes_rpt.auth.enums.ScopeName`. Scopes are checked independently of roles: a token's scopes
are what it can *do*, not a restatement of the principal's role.

## 5. Service-to-service authentication

Service tokens (`principal_type: "service"`) are minted the same way as human tokens but are
expected to carry only the scopes a specific background job or internal caller needs, with a
shorter default TTL path available (`Settings.service_token_ttl_seconds`). Routes meant only
for human operators depend on `require_human`; routes meant only for internal service callers
depend on `require_service`. A service token presented to a human-only route — or vice versa —
is rejected with `AuthorizationError`, not silently allowed.

## 6. Rate limiting

`hermes_rpt.auth.rate_limit.RateLimiter` is an interface with one simple, explicitly
non-distributed implementation (`InMemoryFixedWindowRateLimiter`) for now — see its docstring
for why a real deployment needs a shared-store implementation instead (Phase 3 requirement 9).

## 7. What is not yet built

* No real external OIDC provider integration (local HS256 dev provider only — see ADR-0010).
* No token revocation / `jti` denylist.
* No password storage of any kind exists or is planned — this platform is JWT/token-only, not
  username+password (Phase 3 requirement 12 is satisfied by not implementing password auth at
  all, rather than implementing it "safely").
