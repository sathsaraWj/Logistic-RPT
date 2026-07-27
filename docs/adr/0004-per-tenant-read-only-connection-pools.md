# ADR-0004: One connection pool per tenant connection; all customer-database access is read-only

## Status

Accepted

## Context

Hermes-RPT connects to customer-owned operational databases it does not control the schema or
uptime of. Sharing a connection pool across tenants (e.g. one generic PostgreSQL pool
parameterized by a runtime credential) is the most resource-efficient approach, but it creates a
real risk of a session/credential mix-up under concurrency, and makes it hard to apply per-tenant
limits or to definitively evict access when a credential is rotated or a connection disabled.
Allowing any write access to a customer database also expands blast radius far beyond what a
prediction platform needs.

## Decision

Every `CustomerDatabaseConnection` gets its own connection pool, keyed by
(`tenant_id`, connection id), created by the connector service and never shared. All sessions
against customer databases are opened as read-only transactions, with connection and statement
timeouts and a concurrency limit. There are no unrestricted raw-SQL endpoints against customer
databases; all access goes through the allowlisted schema/table discovery, mapping, and feature
extraction paths. Pools are evicted immediately when a credential reference is rotated or a
connection is disabled.

## Consequences

* Higher resource overhead (many small pools instead of one large one) — acceptable given the
  platform's scale target (customer count, not request-per-second-per-tenant volume) and
  revisited only if it becomes a measured bottleneck.
* A bug or exploit cannot escalate to writing into a customer's operational system, by
  construction, since the transaction itself is read-only.
* Credential rotation has a clean, testable "no stale pool keeps working" behavior.
* Enables straightforward per-tenant integration testing (Tenant Alpha vs Tenant Beta databases,
  Phase 4) since pools are already isolated units.
