# ADR-0010: JWT-based auth with a pluggable `TokenVerifier`; HS256 local-dev provider now, OIDC/JWKS later

## Status

Accepted

## Context

Phase 3 needs production-oriented authentication now, without yet having a real external
identity provider to integrate against. Building a throwaway auth scheme for development and a
different, incompatible one for production would mean the security-critical code path is never
actually exercised by local development or CI.

## Decision

Define a `TokenVerifier` protocol (`hermes_rpt.auth.verifier.TokenVerifier`) with a single
`verify(token) -> TokenClaims` method that checks signature, issuer, audience, expiry, and
required claims, and raises `AuthenticationError` (never a claim-specific, detail-leaking
exception) on any failure. `LocalDevTokenVerifier` implements it with HS256 and a shared secret
from settings — adequate for local development and CI, explicitly not for staging/production
(`Settings` refuses to start in those environments with the insecure default secret still set).
A real deployment implements the same protocol against an external OIDC provider's JWKS
endpoint (RS256, rotating keys, no shared secret); that adapter is not built in this repository
yet. All token creation/verification goes through PyJWT — no hand-rolled cryptography (Phase 3
requirement 11).

`hermes_rpt.auth.dev_tokens.issue_dev_token` mints tokens the local verifier accepts, for local
development and the test suite only; it refuses to run outside `local`/`ci` environments.

## Consequences

* The exact code path (claim validation, `TenantContext` resolution, scope/role checks) that
  runs in production also runs in every test — no "it worked with the fake auth" gap.
* Swapping in a real OIDC provider later is an adapter implementing `TokenVerifier`, not a
  rewrite of anything that consumes `TokenClaims`.
* The insecure default secret is a real footgun if the "refuse to start" guard in `Settings`
  were ever removed — that guard is load-bearing and should not be treated as boilerplate.
* This ADR does not choose a specific production identity provider; that decision is deferred
  (see TASKS.md unresolved assumptions).
