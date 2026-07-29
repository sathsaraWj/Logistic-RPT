# Hermes-RPT Threat Model

Status: threat model for the architecture in [ARCHITECTURE.md](ARCHITECTURE.md), validated
against a dedicated repository-wide security review in Phase 16 — see
[docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) for concrete findings, fixes, and adversarial test
coverage against every threat entry below.

## 1. Primary concern

Hermes-RPT's central risk is **cross-tenant data exposure**: any path by which Tenant A could
read, infer, or influence Tenant B's database contents, schema, mappings, features, datasets,
model adapters, predictions, or logs. Every other threat in this document is secondary to that
one; every phase's test suite is expected to include negative tests proving isolation holds for
whatever surface that phase adds.

## 2. Assets

| Asset | Sensitivity | Notes |
|---|---|---|
| Customer DB credentials / connection strings | Critical | Never persisted resolved, never logged, never returned via API |
| Customer operational data (read via connector) | Critical | Not copied wholesale; only mapped/aggregated features persist |
| Schema snapshots & fingerprints | High | Reveal customer table/column structure |
| Schema mappings | High | Reveal how a customer's business maps to canonical concepts |
| Extracted features / datasets | High | Derived customer data, still tenant-confidential |
| Trained tenant adapters / fine-tuned weights | High | Can memorize tenant-specific patterns |
| Predictions & explanations | Medium-High | Business-sensitive; must not leak other tenants' identifiers |
| Audit logs | Medium | Security-relevant; must themselves be tenant-scoped for reads |
| Shared base model weights | Medium | Must not embed tenant-private adapter weights |
| Platform control-plane credentials/service tokens | Critical | Compromise affects all tenants |

## 3. Actors

* **Platform Admin** — Hermes staff, cross-tenant administrative access, itself audited.
* **Tenant Admin / Data Steward / ML Engineer / Manager / Operator / Read-only Auditor** —
  tenant-scoped human roles with different permission scopes.
* **Service Account** — machine identity for internal service-to-service calls and background
  jobs; must be distinguishable from a human token.
* **Malicious or compromised tenant** — a legitimate but hostile or compromised customer
  attempting to access another tenant's resources, or to abuse the mapping/feature layer to
  exfiltrate data via query cost or malformed expressions.
* **External attacker** — no valid credentials; attacks the perimeter (auth, exposed
  endpoints, dependency vulnerabilities).
* **Compromised background job / worker** — a job that received the wrong or absent tenant
  context and processes/writes data under a wrong identity.
* **External Partner Integration** — a separate product's backend (e.g. Hermes VMS)
  authenticating via a tenant-scoped `ServiceCredential` (`docs/AUTHENTICATION.md` §5a), not a
  human and not an internal Hermes-RPT service. Distinct from "Service Account" above: it
  crosses an organizational trust boundary, not just a process boundary, and its credential is
  independently revocable per integration rather than backed by the platform's own signing key
  the way an internal service token is.

## 4. Trust boundaries

```text
[External attacker] ──(public internet)──▶ [API edge] ──▶ [AuthN/AuthZ] ──▶ [TenantContext]
                                                                        │
[Tenant Admin/user] ──(authenticated)────────────────────────────────┘
                                                                        │
                                              ┌─────────────────────────▼─────────────────────┐
                                              │        Hermes-RPT control plane (trusted)       │
                                              │  (services, repositories, control-plane DB)     │
                                              └───────────────────┬─────────────────────────────┘
                                                                   │ per-tenant, read-only,
                                                                   │ resolved-at-request-time
                                                                   ▼
                                              [Tenant N's customer database] (untrusted content,
                                               trusted-but-isolated connection)
```

Key boundary: everything to the right of "AuthN/AuthZ" trusts the resolved `TenantContext`
completely — which is exactly why claim verification and never trusting client-supplied tenant
identifiers is the single most load-bearing control in the system (ADR-0003).

A second boundary sits between the control plane and each customer database: the connector
service treats *customer data itself* as untrusted content (it must not be interpreted as SQL,
code, or mapping-expression syntax), even though the *connection* to it is trusted and
tenant-bound.

## 5. Threats (STRIDE-oriented) and mitigations

### Spoofing

* **T-S1**: Forged/replayed auth token. *Mitigation*: signature, issuer, audience, expiry
  validation (Phase 3); no custom crypto.
* **T-S2**: Client sets its own tenant via header/query/body. *Mitigation*: tenant resolved only
  from verified claims; any client-supplied tenant identifier used only as a value to be
  compared against the trusted one, never trusted directly (ADR-0003).
* **T-S3**: A service token used as if it were a human token (e.g. to bypass per-user scopes).
  *Mitigation*: distinct token types/claims for service-to-service auth; explicit test in
  Phase 3.
* **T-S4**: A leaked/exfiltrated external-partner `ServiceCredential` secret used to obtain
  tokens as that tenant. *Mitigation*: revocable at the credential layer independently of the
  platform's global signing key — `is_active=False` stops all future exchanges immediately;
  blast radius is bounded to one tenant's one integration and at most one
  `service_token_ttl_seconds` window for any token already issued before revocation (see
  `docs/AUTHENTICATION.md` §5a for why a full `jti` denylist wasn't built for this instead).
  Constant-time secret comparison and a dummy-hash comparison on an unknown `client_id` reduce
  (do not eliminate) the timing side channel that would otherwise distinguish "wrong secret"
  from "no such client" — same residual-risk framing as T-I5.

### Tampering

* **T-T1**: Mapping-expression injection to reach unauthorized columns/tables or execute
  arbitrary logic. *Mitigation*: restricted declarative expression language, no arbitrary
  Python/SQL (Phase 7), query compiler validates against the allowlisted mapped objects only.
* **T-T2**: SQL injection via feature extraction or discovery. *Mitigation*: fully parameterized
  queries, no string-built SQL from user/customer-controlled values, query validator/cost guard
  (Phase 8).
* **T-T3**: Model artifact substitution / unsafe deserialization. *Mitigation*: safe
  serialization formats, no untrusted pickle loading, checksum verification in the registry
  (Phase 14, hardened in Phase 16).

### Repudiation

* **T-R1**: An action (schema change acknowledgment, mapping approval, prediction) cannot later
  be attributed. *Mitigation*: `AuditEvent` for every access/lifecycle change/prediction,
  including principal, tenant, correlation ID, and outcome.

### Information disclosure

* **T-I1**: Cross-tenant read via a forgotten `tenant_id` filter. *Mitigation*: repository-layer
  enforcement + RLS defense in depth + mandatory negative tests per phase (ADR-0003).
* **T-I2**: Cross-tenant cache collision (same cache key derived from a business ID only).
  *Mitigation*: all cache keys namespaced by `tenant_id`; tested explicitly (Phase 16).
  Cross-tenant cache/dataset/prediction-request contamination is one of the security invariants
  restated in the implementation plan.
* **T-I3**: Secrets or connection strings appear in logs/exceptions. *Mitigation*: structured
  logging with redaction (Phase 1), secret provider never returns resolved secrets via API
  (Phase 4), explicit "no secrets in logs" tests.
* **T-I4**: Verbose error messages leak internals (stack traces, table names, driver errors) to
  clients. *Mitigation*: safe error mapping at the API boundary (Phase 3+).
* **T-I5**: Query cost / timing side channel reveals another tenant's data existence.
  *Mitigation*: per-tenant, per-connection query limits, timeouts, and concurrency limits
  (Phase 4); out of scope to fully eliminate timing side channels in this phase — recorded as a
  residual risk below.
* **T-I6**: Profiling during schema discovery captures PII/secrets. *Mitigation*: sampling
  limits, denylisted column-name patterns, masking, summaries-only storage, audit of every
  profiling run (Phase 5).
* **T-I7**: Pretraining objectives memorize and later regurgitate identifiers through inference.
  *Mitigation*: canary-based memorization tests, identifier removal/hashing policy,
  tenant-isolated pretraining by default (Phase 12).

### Denial of service

* **T-D1**: Expensive/unbounded schema discovery or feature extraction against a customer
  database. *Mitigation*: row/time-window limits, statement timeouts, concurrency limits,
  query-cost guard.
* **T-D2**: A single tenant's connection pool exhaustion affecting others. *Mitigation*:
  per-tenant pools with bounded size; no shared pool to exhaust.
* **T-D3**: Brute force / enumeration of the service-credential token-exchange endpoint
  (`POST /v1/auth/service-token`) — the one endpoint whose body *is* the authentication, so it
  has no bearer-token check to fail fast on first. *Mitigation*: per-`client_id` and per-source-IP
  rate limiting (`docs/AUTHENTICATION.md` §5a). Online brute force of the secret itself is
  infeasible at its entropy regardless; the limiter's actual job is bounding noise/DoS and
  credential-stuffing against a specific `client_id`. Inherits the existing non-distributed
  limitation already noted under §6.

### Elevation of privilege

* **T-E1**: Missing permission check on a lifecycle transition (e.g. activating a mapping,
  promoting a model) allows an under-privileged role to act. *Mitigation*: explicit
  role/scope checks per mutating endpoint, tested per role (Phase 3, 7, 14).
* **T-E2**: A tenant's adapter is loaded for a different tenant's inference request.
  *Mitigation*: adapter lookup keyed by authenticated tenant only, tested explicitly (Phase 14).
* **T-E3**: Row-Level Security misconfiguration is treated as sufficient and app-layer checks
  are skipped. *Mitigation*: ADR-0003 makes RLS explicitly "defense in depth, not primary."

## 6. Residual risks / explicit assumptions (to revisit in Phase 16)

* Full elimination of timing/size side channels across tenant boundaries is not attempted in
  the initial phases; only gross query-cost limits are enforced.
* The local development secret provider is not suitable for production; cloud KMS adapters
  (Azure Key Vault, Google Secret Manager) are interface-only until a later phase.
* No production cloud deployment, network segmentation, or WAF is built in Phases 0–18; the
  threat model assumes those will be layered on before any real customer data is connected.
* Physical/infrastructure-level threats (cloud provider compromise, insider threat with direct
  database access) are out of scope for an application-level threat model.
* `service_credentials`' RLS policy has a narrow, deliberate carve-out (an unbound session, with
  no tenant set at all, sees every row) so the token-exchange flow can resolve which tenant a
  `client_id` belongs to before a `TenantContext` exists. Every other strict tenant-owned table
  denies an unbound session all rows. This is invisible today (RLS is a structural no-op
  platform-wide — the connecting role has `BYPASSRLS`, tracked separately in
  `docs/SECURITY_REVIEW.md`), but remains an intentionally-weaker defense-in-depth layer for this
  one table even after that finding is fixed — the primary control (application-layer
  `find_by_client_id`, scoped by `client_id`'s global uniqueness, not by tenant) is what this
  table actually relies on, exactly as ADR-0003 frames RLS everywhere else.

## 7. How this document is used

Every phase that adds a new capability is expected to add or update the corresponding threat
entries and mitigations here, and Phase 16 performs a dedicated, whole-repository security
review against this model plus new adversarial tests (see `docs/SECURITY_REVIEW.md`, produced
in that phase).
