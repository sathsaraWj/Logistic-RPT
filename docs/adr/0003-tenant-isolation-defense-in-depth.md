# ADR-0003: Tenant isolation is enforced at every layer independently; Row-Level Security is defense in depth, never the sole control

## Status

Accepted

## Context

Cross-tenant data exposure is the platform's primary risk (see `docs/THREAT_MODEL.md`).
PostgreSQL Row-Level Security (RLS) can enforce tenant filtering at the database layer, which is
attractive because it applies even if application code has a bug. But RLS alone has failure
modes: it depends on the connection's session variable/role being set correctly on every
connection, `BYPASSRLS` roles or migrations can disable it, and RLS policies are easy to get
subtly wrong for complex joins — none of which show up as an error, only as silent data leakage.

## Decision

Tenant isolation is enforced at every layer described in `docs/ARCHITECTURE.md` §4
independently: verified-token tenant resolution, service-layer checks, mandatory repository-layer
`tenant_id` filtering (API handlers never query ORM models directly), per-tenant connection
pools, tenant-namespaced caches/datasets/features, and tenant-scoped model adapter lookup. RLS
is additionally enabled on tenant-owned control-plane tables as one more layer, but no service is
permitted to omit its own application-layer tenant filter on the assumption that "RLS will catch
it." Every phase that touches tenant-owned data must include an automated negative test proving
Tenant A cannot read/write Tenant B's records, independent of whether RLS is enabled.

## Consequences

* Higher implementation cost per phase (explicit filters + tests), but no single point of
  failure for the platform's most important invariant.
* RLS misconfiguration becomes a defense-in-depth gap, not a full breach, if the application
  layer is correct — and vice versa.
* Requires discipline: code review and CI should treat a tenant-scoped repository method
  without a `tenant_id` filter as a blocking defect, not a style nit.
