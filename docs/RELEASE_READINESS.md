# Release Readiness (Phase 18)

An early, secure, multi-tenant relational machine-learning and transformer research platform
for fleet and logistics data. **Not** equivalent to, compatible with, or a replacement for any
third-party "SAP-RPT"-style product — Hermes-RPT is a distinct, from-scratch research
architecture named for its own design, evaluated in this document on its own terms.

This is the honest, current snapshot [README.md](../README.md) points to. It supersedes any
more optimistic framing implied elsewhere; where another document and this one disagree, this
one is more current (dated by the phase that last touched it) and should be treated as
authoritative for a go/no-go decision.

## 1. Completed capabilities

* **Multi-tenant platform core**: tenant/user/membership model, role/scope-based authorization,
  verified-JWT-only tenant resolution (never client-supplied), audit trail for every access,
  lifecycle change, and prediction (Phases 1–3).
* **Customer database connectivity**: read-only, per-tenant connection pools; register/validate/
  enable/disable/rotate-secret/delete lifecycle; statement-shape and schema/table-allowlist
  guards; PostgreSQL supported and load-bearing-tested, MySQL/MSSQL/Oracle are interface
  reservations only (Phase 4).
* **Schema discovery and drift detection**: versioned, fingerprinted snapshots; optional bounded/
  masked profiling; drift comparison against the prior snapshot; affected-mapping suspension,
  never silent auto-rewrite (Phase 5).
* **Canonical ontology and declarative mapping**: a customer-agnostic fleet/logistics vocabulary
  (`configs/ontology/v1.yaml`); a restricted, non-Turing-complete mapping/derived-expression
  language (no arbitrary SQL or Python reachable from a mapping document); full lifecycle with
  approval gates (Phases 6–7).
* **Safe feature extraction and dataset building**: parameterized, allowlisted, point-in-time-
  correct queries; leakage-safe feature contracts; manifested, checksummed, lineage-tracked
  datasets stored outside the repository (Phases 8–9).
* **Baseline models**: logistic regression, gradient-boosted trees, small MLP, with a shared
  comparison report, MLflow tracking, and (Phase 17-hardened) correct behavior when a feature is
  100%-missing for a tenant (Phase 10).
* **Hermes-RPT-0.1 (Tiny)**: a real relational transformer — explicit entity/relationship
  embeddings, missing-value embeddings (never fake zeros), bounded/masked relational context,
  self-supervised pretraining objectives (masked-cell, link-prediction, temporal-order), tenant-
  isolated adapters on a frozen shared backbone (Phases 11–12, 14).
* **Model registry and governance**: candidate/staging/production/archived stages, named
  aliases, atomic rollback (repointing, distinctly audited), adapter/base compatibility checks,
  artifact-integrity checksums re-verified at load time — not just at registration (Phases 10,
  14, hardened Phase 16).
* **Inference API**: a real, documented HTTP endpoint (`POST /v1/predictions/delivery-delay`),
  safe explanations (never raw feature dump), idempotency, timeout/retry/circuit-breaker around
  the one genuine external dependency (MLflow artifact loading) (Phase 13).
* **Monitoring and drift detection**: Prometheus-compatible metrics (label-safe — never a raw
  business/customer ID), separate operator-only (`/metrics`) and tenant-scoped
  (`/v1/monitoring/summary`) surfaces, realized precision/recall from recorded outcomes, three
  operational runbooks (Phase 15).
* **Security hardening**: three-way parallel audit across 18 review areas; every Critical/High
  finding fixed with a named regression test; adversarial test coverage for all 16 named attack
  vectors from prompts.txt; four required security docs (Phase 16).
* **End-to-end demonstration**: two tenants with genuinely different schemas, real separate
  PostgreSQL databases, the real HTTP API — discovery, mapping, training, prediction with
  lineage, and a five-part security-test step, all verified to actually run, not just designed
  (Phase 17). Running this for real surfaced and fixed several previously-latent bugs (see
  [CHANGELOG.md](../CHANGELOG.md) Phase 17 entry) — the kind of gap that only running the full
  system for real, not reading the code, can find.
* **Real GCP deployment**: `apps/api` runs on Cloud Run against Cloud SQL, with Workload Identity
  Federation (no long-lived key) and Secret Manager, via a GitHub Actions CD pipeline
  ([docs/DEPLOYMENT.md](DEPLOYMENT.md)) — not merely a design target.

## 2. Known limitations

* **No real customer data has ever been used.** Every dataset, every training run, every
  demonstration uses synthetic Tenant Alpha/Beta data. Every model-quality claim in
  [docs/MODEL_RESEARCH_PLAN.md](MODEL_RESEARCH_PLAN.md) §9 is explicitly inconclusive for exactly
  this reason — the synthetic label generator doesn't correlate strongly enough with available
  features to distinguish a good model from a mediocre one, for baselines and the transformer
  alike.
* **`apps/worker` is a placeholder.** Background jobs (schema discovery) run in-process via
  `asyncio.create_task` from `apps/api` itself, not on a durable queue — a process restart mid-job
  loses it (the snapshot is left `RUNNING` forever). `apps/trainer` is a local CLI, never
  deployed or scheduled.
* **Only `LocalDevSecretProvider` is implemented.** Azure Key Vault and Google Secret Manager
  providers are interface reservations (`hermes_rpt.secrets.provider`) — real deployments today
  use Secret Manager's *own* injection into Cloud Run env vars ([docs/DEPLOYMENT.md](DEPLOYMENT.md)
  §4), not this platform's own secret-provider abstraction, which stores customer *connection*
  credentials, a different concern.
* **Only PostgreSQL customer databases are supported.** MySQL/MSSQL/Oracle connectors raise
  `UnsupportedEngineError` — interfaces exist, implementations do not.
* **No self-service tenant onboarding.** Tenant creation has no public API by design
  ([docs/DEPLOYMENT.md](DEPLOYMENT.md) §2) — an operator provisions tenants directly today.
* **No data retention/deletion automation** — see [docs/DATA_PROTECTION.md](DATA_PROTECTION.md)
  §7.
* **A handful of Medium/Low security findings are deliberately deferred** with stated reasoning
  — see [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) §3 (no pagination limits on a few
  control-plane list endpoints, no cap on schema-discovery result size, stdlib-logger redaction
  bridging, and others).

## 3. Security status

See [docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) for full detail. Summary:

* **One Critical finding is open**: the database role this platform connects as has
  `BYPASSRLS`, making every PostgreSQL Row-Level Security policy a structural no-op, in local dev
  and very likely in the deployed production instance too (§2a). This does **not** mean a
  cross-tenant leak has occurred — the *primary* control (application-layer `tenant_id`
  filtering) is unaffected and has its own extensive, passing test suite
  (`tests/security/test_tenant_isolation.py` and others) that does not depend on RLS. It means
  the specific "if the primary filter is ever forgotten, RLS independently still stops the leak"
  guarantee does not currently hold — a real gap, and the top-priority item in §4 below.
* **Every other Critical/High finding from the Phase 16 review is fixed**, each with a named
  regression test: tenant-identity spoofing in dataset building, unscoped model-registry
  mutations, model artifact substitution, disabled connections still usable, and
  `/metrics` cross-tenant exposure.
* **All 16 named adversarial attack vectors from prompts.txt have test coverage** (SQL injection,
  mapping-expression injection, path traversal, model artifact substitution, unsafe
  deserialization, cross-tenant cache keys/worker jobs/adapter loading, forged tenant headers,
  invalid JWT claims, secrets in logs, stale mapping use after drift, and others).
* **A handful of Medium/Low findings are explicitly deferred**, each with stated reasoning, not
  silently dropped (§3 of the security review).

## 4. Model-performance status

Every engineering/architecture claim in the research plan is validated: the same pipeline runs
unmodified across heterogeneous schemas, relational context is real (not a flattened feature
vector), tenant adapters are structurally isolated (proven by assertion against the real
governance layer, not just by inspection), and every training run is reproducible given a fixed
seed.

**Phase 19 update**: the synthetic label generator was strengthened to be risk-weighted (distance,
vehicle age, breakdown/route/driver history — see
[docs/BASELINE_MODELS.md](BASELINE_MODELS.md) §10) specifically to make a real baseline
comparison possible; the original generator's label was statistically independent of every
feature, so nothing before this could be more than noise. Re-run against the corrected generator,
`hermes-rpt-0.1-tiny-scratch` beat every Phase 10 baseline on PR-AUC consistently across three
training seeds (0.3053–0.3633 vs. the best baseline's 0.2793 — full table in
[docs/MODEL_RESEARCH_PLAN.md](MODEL_RESEARCH_PLAN.md) §9) and was promoted to `PRODUCTION`.

**What this does and does not validate**: it's real evidence the architecture can exploit
relational context when the data has learnable structure. It is **not** evidence about real
fleet operations — the correlation that makes the label learnable was this phase's own
engineering choice, not something observed in real customer data, and pretraining-vs-scratch
and tenant-adapter quality questions remain exactly as open as before (§9 point 3/4). Whether
Hermes-RPT actually predicts delivery delay well on a real customer's data is still genuinely
open. See [docs/MODEL_RESEARCH_PLAN.md](MODEL_RESEARCH_PLAN.md) §9 for the full, honest
retrospective.

## 5. Required infrastructure

For a real deployment beyond the current single-project GCP demo
(`hermes-rpt-demo`, [docs/DEPLOYMENT.md](DEPLOYMENT.md)):

* A production-grade Cloud SQL instance (current: `db-g1-small`, a demo-sized tier) with the
  two-role privilege split from §6 below applied before any real tenant data is connected.
* A durable job queue for `apps/worker` (Cloud Tasks, Pub/Sub, or similar) replacing the current
  in-process `asyncio.create_task` scheduling.
* A real cloud secret provider wired into `hermes_rpt.secrets.provider` (Google Secret Manager
  or Azure Key Vault) for *customer connection credentials* specifically — distinct from the
  platform's own JWT secret/DB password, which already use Secret Manager.
* GPU-backed compute for any future training beyond the currently-trained Tiny transformer size.
* A tenant-onboarding process (still deliberately no self-service API — an operator workflow, or
  a built one, either way not yet built).
* Network segmentation / WAF in front of Cloud Run, not assumed by the current threat model
  ([docs/THREAT_MODEL.md](THREAT_MODEL.md) §6).

## 6. Production blockers

In priority order:

1. **RLS BYPASSRLS finding (§3)** — fix the connecting database role's privileges (a two-role
   split: a privileged role for Alembic migrations only, a `NOSUPERUSER NOBYPASSRLS` runtime
   role granted only DML) in both local dev and the deployed Cloud SQL instance, and re-verify
   `tests/integration/test_row_level_security.py` actually passes against both.
2. **No real customer data has validated any model-quality claim.** Hermes-RPT-0.1 (Tiny, scratch)
   is now servable and, per §4, beat the baselines on synthetic data with a deliberately
   strengthened label — that is evidence the architecture works, not evidence about real fleet
   operations. Do not serve either Hermes-RPT or the baselines to make real predictions that
   matter until at least one design partner's consented data has gone through the full pipeline
   and the resulting metrics have been evaluated against the "stop or change direction" criteria
   in [docs/MODEL_RESEARCH_PLAN.md](MODEL_RESEARCH_PLAN.md) §7. This blocker applies to both
   model families equally.
3. **`apps/worker` needs a durable queue** before any background job (schema discovery today,
   more later) can be trusted not to silently vanish on a process restart.
4. **Real secret-provider adapters** (Key Vault / Secret Manager, for *customer* credentials)
   before any real customer database connection is registered — `LocalDevSecretProvider` is
   explicitly local-dev-only.
5. **Deferred Medium/Low security findings** ([docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md) §3)
   should be triaged and scheduled, not left indefinitely deferred.

## 7. Recommended next milestone

**Secure one real, consented design-partner dataset** (or a close proxy — anonymized/synthetic-
from-real-distribution data a real fleet operator has reviewed) and re-run the full Phase 9–14
pipeline against it, before investing further in synthetic-data-only experimentation or in
**Small**/**Base** model sizes. This is the single highest-leverage next step: it is the one
thing that can actually answer the model-quality research questions §1 of the research plan
posed and this document's §4 confirms are still open, and it will likely surface additional
real-schema edge cases the synthetic fixtures haven't. Do this *before*, not instead of, fixing
production blocker #1 (§6) — both matter, but #1 is a small, well-understood, low-risk fix,
while this milestone is the actual point of the platform.
