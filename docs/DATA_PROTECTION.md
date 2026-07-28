# Data Protection

Status: current as of Phase 16 (Security Hardening). Describes what data this platform holds,
how it's classified, and the concrete controls protecting it at rest, in transit, in logs, and
across tenant boundaries. Companion to [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) (what was
checked and fixed) and [docs/THREAT_MODEL.md](THREAT_MODEL.md) (why these controls exist).

## 1. Data classification

Reusing the asset table from [docs/THREAT_MODEL.md](THREAT_MODEL.md) §2, with the concrete
handling rule for each:

| Data | Classification | Where it lives | Handling rule |
|---|---|---|---|
| Customer DB credentials | Critical | Secret Manager / Azure Key Vault only, referenced by a `credential_reference` string in `CustomerDatabaseConnection` | Never persisted resolved in this platform's own database; never returned by any API response; never logged (§4) |
| Customer operational data (raw rows) | Critical | Never persisted here at all | Read live, per-request, through a tenant-bound connection; only mapped/aggregated *features* or profiling *summaries* are ever written to the control-plane DB |
| Schema snapshots & fingerprints | High | Control-plane DB, `tenant_id`-scoped | Repository-layer + RLS tenant filtering (§3) |
| Schema mappings | High | Control-plane DB, `tenant_id`-scoped | Same |
| Extracted features / datasets | High | Control-plane DB / object storage, `tenant_id`-scoped | Same; `DatasetTenantMismatchError` guards `DatasetBuildService.build()` against a spoofed tenant identity (see SECURITY_REVIEW.md finding 1) |
| Trained tenant adapters | High | MLflow artifact store, path includes `tenant_id` | Adapter lookup keyed by authenticated tenant only, never a client-supplied one |
| Predictions & explanations | Medium-High | Control-plane DB, `tenant_id`-scoped | Same repository/RLS pattern |
| Audit logs | Medium | Control-plane DB, `tenant_id`-scoped (nullable for platform-level pre-auth events) | RLS policy admits `NULL`-tenant platform events and real-tenant events, nothing else (fixed in Phase 16 — see SECURITY_REVIEW.md) |
| Shared base model weights | Medium | MLflow artifact store, no tenant prefix | Frozen at adapter-training time (`requires_grad=False` on every backbone parameter, `torch.no_grad()` forward pass); serializes only the adapter's own `state_dict()`, never the backbone's |
| Platform control-plane credentials/service tokens | Critical | Secret Manager / env vars injected at deploy time | Never committed, never logged; `jwt_secret_key` is a `pydantic.SecretStr` |

## 2. Encryption

* **In transit**: Cloud Run terminates TLS at the edge for all inbound traffic
  ([docs/DEPLOYMENT.md](DEPLOYMENT.md) §1). Connections *to* customer databases use
  `tls_mode` on `CustomerDatabaseConnection` (`require` by default); the connector refuses to
  downgrade below what's configured.
* **At rest**: the control-plane Cloud SQL instance and the Secret Manager secrets it depends on
  are encrypted at rest by the underlying GCP service by default (Cloud SQL, Secret Manager) —
  this platform does not implement its own at-rest encryption layer on top; there is nothing
  application-level to configure here beyond not disabling GCP's defaults.
* **Secrets specifically**: never encrypted-and-stored by this platform's own code at all — see
  §3. The only place a secret *value* is ever resolved into plaintext is inside the connector
  service, for the duration of building a database connection; it is never written back to any
  store this platform controls.

## 3. Secret handling

`hermes_rpt.secrets.provider.SecretProvider` is the only interface anything in this codebase
uses to resolve a credential; there are three implementations:

* `LocalDevSecretProvider` — in-memory, for local development and tests only.
* `AzureKeyVaultSecretProvider` / `GoogleSecretManagerSecretProvider` — resolve a
  `credential_reference` against the real secret store at request time.

`CustomerDatabaseConnection` stores only the `credential_reference` string (which secret to
fetch), never the resolved value. A resolved secret exists in memory only for the lifetime of
building the connection (`ConnectionLifecycleManager`/`get_or_create_engine`) and is never
written to the control-plane database, never included in an API response
(`ConnectionTarget.password` has `field(repr=False)` specifically so an accidental `repr()`/log
of the object can't leak it — see SECURITY_REVIEW.md), and never cached beyond the connection
pool's own live engine object.

## 4. Logging and redaction

`hermes_rpt.common.logging` installs a mandatory structlog processor
(`_redact_processor`) on every log call in this codebase:

* **Key-based redaction**: any field whose key matches a sensitive-key pattern (`password`,
  `secret`, `token`, `credential`, `authorization`, `email`, `display_name`, `phone_number`,
  etc.) is replaced with `***REDACTED***` regardless of its value's shape.
* **Value-based redaction**: free-text fields are scanned for connection-string credentials
  (`postgresql://user:pass@...`), keyword-style DSN fragments (`password=...`), bearer tokens,
  and email addresses, even under an innocuous-looking key like `"note"`.
* **Structural recursion**: nested dicts, lists, and tuples are redacted element-by-element, not
  just at the top level.
* **Opaque-object fallback**: anything that isn't a string/mapping/list/tuple/safe scalar has
  its `repr()` scanned against the same value patterns — added in Phase 16 specifically so an
  object like `ConnectionTarget` logged under a safe key still gets caught even if a future
  field forgets `repr=False`.

What this does **not** currently cover: SQLAlchemy's, asyncpg's, and uvicorn's own stdlib
loggers are not bridged through this same processor chain — recorded as a deferred hardening
item in [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) §3, not a currently-exploited gap (neither
this platform's code nor those libraries log credential values through that path today).

Test coverage: `tests/unit/test_logging_redaction.py`.

## 5. Tenant isolation (the control this platform leans on hardest)

Three independent layers, deliberately redundant (ADR-0003):

1. **Application layer (primary)**: `TenantContext` is built only from verified JWT claims —
   never from a client-supplied header, query param, or body field. Every repository extends
   `TenantScopedRepository`, which auto-filters every query by `tenant_id` and raises
   `TenantMismatchError` (mapped to `404`, never `403`, so a cross-tenant probe can't
   distinguish "doesn't exist" from "exists but isn't yours") on any attempted cross-tenant
   access.
2. **Database layer (defense in depth)**: PostgreSQL Row-Level Security, bound per-session via
   `bind_tenant_for_row_level_security` — every request-scoped session
   (`hermes_rpt.auth.dependencies.get_tenant_context`) and, as of Phase 16, every background-job
   session (`hermes_rpt.schemas.jobs.run_discovery_job`) binds `app.current_tenant_id` before
   touching data.
3. **Connection-pool layer**: `TenantConnectionPoolRegistry` keys engines by `(tenant_id,
   connection_id)` — structurally impossible for one tenant's pooled connection to be handed to
   another tenant's request.

No caching layer exists anywhere in this codebase that could introduce a fourth, cache-key-based
leak surface — confirmed in [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) §4.

## 6. Data minimization

* Raw customer rows are **never** persisted in this platform's own storage — only mapped
  features, aggregate profiling summaries, and model artifacts derived from them.
* Profiling (`hermes_rpt.schemas.profiling`) is off by default, bounded (`max_tables` ≤20,
  `sample_rows` ≤200), and structurally cannot profile a column matching a credential-name
  pattern; personal-data-shaped columns get aggregate stats only, never sample values.
* Audit events store platform IDs (`tenant_id`, `connection_id`, `prediction_id`), not raw
  customer data — see [docs/runbooks/INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md) §4 for
  the same principle applied to incident communications.

## 7. Retention

No automated retention/deletion job exists yet for control-plane data (schema snapshots,
mappings, features, datasets, predictions, audit events) — everything persists until explicitly
deleted through the API or database administration. This is a known gap, consistent with
[docs/THREAT_MODEL.md](THREAT_MODEL.md) §6's residual-risk framing: acceptable for the demo/MVP
scope this repository targets, not something to assume in a real production deployment without
a retention policy layered on top.

## 8. Related documents

* [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) — the audit this document's controls were
  validated against.
* [docs/THREAT_MODEL.md](THREAT_MODEL.md) — the asset/threat model behind the classifications in
  §1.
* [docs/CUSTOMER_DATABASE_GUIDE.md](CUSTOMER_DATABASE_GUIDE.md) — what a customer needs to set up
  on *their* side (read-only role, allowlist) for §5's controls to hold.
* [docs/INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md) — what happens if one of these controls ever
  fails.
