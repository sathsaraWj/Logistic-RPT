# Customer Database Connection Guide

Status: written in Phase 16 (Security Hardening), documenting how a tenant should safely connect
their own operational database to Hermes-RPT, and exactly what the platform will and will not do
with that connection once it exists. Read this alongside
[docs/DATA_PROTECTION.md](DATA_PROTECTION.md) (what happens to your data once connected) and
[docs/SCHEMA_MAPPING.md](SCHEMA_MAPPING.md) (what happens after discovery, when you map your
schema to Hermes-RPT's ontology).

## 1. What Hermes-RPT needs from you

* **A PostgreSQL connection** (host, port, database name) reachable from wherever this platform
  is deployed.
* **A database user with read-only access**, scoped to only the schemas/tables you want
  Hermes-RPT to see. Concretely: `GRANT USAGE ON SCHEMA <your_schema> TO hermes_reader;
  GRANT SELECT ON ALL TABLES IN SCHEMA <your_schema> TO hermes_reader;` — and nothing broader.
  Hermes-RPT never issues a write statement against your database (`hermes_rpt.connectors.
  query_guard.assert_read_only_statement` rejects `INSERT`/`UPDATE`/`DELETE`/`DROP`/etc. as a
  second, defense-in-depth layer even if the credential you provide happens to have write
  access — but don't rely on that; provision a genuinely read-only role).
* **A schema and table allowlist**: at registration time you tell Hermes-RPT exactly which
  schemas and tables it's allowed to touch (`schema_allowlist`/`table_allowlist` on
  `POST /v1/connections`). An empty allowlist means **nothing** is accessible — the platform
  fails closed, not open. Discovery, profiling, and every later query all re-check every object
  against this allowlist before touching it.
* **TLS** (`tls_mode`, default `require`) — Hermes-RPT does not connect to your database over an
  unencrypted channel unless you explicitly configure a weaker mode, and doing so is not
  recommended.

## 2. Registering a connection

```
POST /v1/connections
{
  "name": "primary-fleet-db",
  "engine": "postgresql",
  "host": "your-db-host.example.com",
  "port": 5432,
  "database_name": "fleet_ops",
  "username": "hermes_reader",
  "secret_value": "<the read-only user's password>",
  "tls_mode": "require",
  "schema_allowlist": ["public"],
  "table_allowlist": ["fleet_vehicle", "fleet_driver", "transport_trip"]
}
```

`secret_value` is written once into your configured secret store (Secret Manager / Key Vault)
and never stored as plaintext in Hermes-RPT's own database, never returned in any API response
afterward, and never appears in logs — see [docs/DATA_PROTECTION.md](DATA_PROTECTION.md) §3–4.
The response never echoes it back.

A new connection starts in `PENDING_VALIDATION`. Two more calls bring it live:

```
POST /v1/connections/{id}/validate   # attempts a real connection, checks reachability + TLS
POST /v1/connections/{id}/enable     # only reachable from a validated connection
```

## 3. What Hermes-RPT does with the connection once enabled

* **Schema discovery** (read-only, metadata only): enumerates table/column shape, keys,
  constraints, indexes, and approximate row counts for schemas on your allowlist —
  `information_schema`/`pg_catalog` queries only, never reads a row of your actual data during
  discovery.
* **Optional profiling** (off by default, opt-in per discovery run): computes small aggregate
  summaries (null fraction, distinct-count estimate, a few sample values) over a bounded sample
  (≤200 rows, ≤20 tables) — never a full table dump, and structurally incapable of profiling a
  column whose name matches a credential pattern (`password`, `secret`, `token`, `api_key`,
  etc.); personal-data-shaped columns (email, phone, address, name, date of birth) get aggregate
  stats only, never sample values.
* **Mapping**: once you (or your data steward) map your schema's columns to Hermes-RPT's
  ontology, feature extraction reads only the columns you've explicitly mapped, through queries
  built from your allowlist — never arbitrary SQL, never a column you haven't mapped.
* **Nothing is ever copied wholesale.** The only things that persist in Hermes-RPT's own storage
  are: the schema *metadata* from discovery, your *mapping definitions*, the *extracted feature
  values* your mappings produce, and (if profiling is enabled) *aggregate* profiling summaries.
  Raw rows from your database are never written to Hermes-RPT's control-plane storage.

## 4. What Hermes-RPT will never do

* Issue a write statement against your database (enforced both by the read-only role you
  provision and by the platform's own statement-shape guard).
* Touch a schema or table you haven't put on your allowlist.
* Return your credential value through any API response, log line, or error message.
* Let another tenant's mapping, query, or prediction request reach your connection — connections
  are pooled per `(tenant_id, connection_id)`, structurally isolated from every other tenant's
  pool (see [docs/DATA_PROTECTION.md](DATA_PROTECTION.md) §5).
* Store your raw operational data outside the mapped-feature/aggregate-profiling boundary
  described in §3.

## 5. Rotating or revoking access

* **Rotate your credential** (recommended periodically, and required after any suspected
  compromise on your side): `POST /v1/connections/{id}/rotate-secret` with the new password.
  The old credential is superseded immediately; nothing about your allowlist or mappings needs
  to change.
* **Temporarily stop access without deleting anything**: `POST /v1/connections/{id}/disable` —
  a disabled connection is refused for every query, including any already-open pool entry (see
  [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) finding 4).
* **Remove the connection entirely**: `DELETE /v1/connections/{id}`.
* If you rotate the password on your database side without calling `rotate-secret` here first,
  the next query will fail with a connection error (visible to you as `status=ERROR` with
  `last_error` set) rather than silently succeeding with a stale credential — see
  [docs/runbooks/INCIDENT_RUNBOOK.md](runbooks/INCIDENT_RUNBOOK.md) §3 for what that looks like
  operationally.

## 6. If you suspect something went wrong on our side

Contact your Hermes-RPT platform contact. Internally, this is handled per
[docs/INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md) — you will be told what happened to *your*
data and what's been done about it; you will not be told about, and will not be asked about,
any other tenant.

## 7. Related documents

* [docs/DATA_PROTECTION.md](DATA_PROTECTION.md) — full data classification and handling detail.
* [docs/SCHEMA_MAPPING.md](SCHEMA_MAPPING.md) — what happens after discovery.
* [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) — the audit behind the guarantees in §4.
* [docs/INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md) — what happens if a guarantee above is ever
  violated.
