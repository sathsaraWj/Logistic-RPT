# Model Registry and Per-Tenant Adaptation

Status: implements Phase 14 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Builds on Phase 10
([BASELINE_MODELS.md](BASELINE_MODELS.md) §6 — the `ModelVersion`/`ModelStage` lifecycle this
phase extends) and reuses Phase 11's `HermesRPTBackbone` ([HERMES_RPT_0_1.md](HERMES_RPT_0_1.md)).

## 1. What "model governance" means here

"Shared Hermes-RPT base + tenant-specific private adapter." Concretely:

* **Shared base models** — `ModelVersion.tenant_id IS NULL` (Phase 10, unchanged).
* **Tenant-specific models** — `ModelVersion.tenant_id` set (Phase 10, unchanged).
* **Tenant-specific adapters** — `TenantModelAdapter`, always tenant-owned (Phase 2/10 schema;
  this phase is the first to actually train and serve one — see §3).
* **Model aliases** — `ModelAlias`, new this phase (§2).
* **Approval workflow** — promoting to `STAGING`/`PRODUCTION`, or pointing an alias, requires
  `ScopeName.MODEL_PROMOTE` (§4).
* **Rollback** — repointing a `ModelAlias` back to a prior target (§2).
* **Model deactivation** — `ModelRegistryService.deactivate_model_version` /
  `.deactivate_adapter`, both audited (§4).
* **Compatibility checks** — `IncompatibleAdapterError`, raised whenever an adapter's
  `ontology_version`/`feature_contract_version` doesn't match the base model it's paired with
  (§4).

## 2. Why aliases exist alongside `stage`

Phase 13's serving path resolves a model purely from `ModelVersion.stage == PRODUCTION`
(`ModelVersionRepository.get_production_model`). That's enough when a base model always serves
alone, but Phase 14 needs to express something `stage` cannot: *which adapter* goes with a shared
base model *for this tenant*. `ModelAlias` (`hermes_rpt.registry.models.ModelAlias`) is a named,
atomically-repointable pointer to exactly one `(ModelVersion, TenantModelAdapter | None)` pair,
scoped either to a tenant or shared (`tenant_id IS NULL`, same nullable-shared convention as
`ModelVersion.tenant_id`) — a tenant's own alias always wins over the shared one of the same
name, same precedence rule `get_production_model` already used.

**Rollback is repointing.** `ModelRegistryService.rollback_alias` is mechanically identical to
`set_alias` — same validation, same upsert-in-place (`(tenant_id, task_definition_id,
alias_name)` stays a single row; uniqueness is enforced in the service layer, not a DB
constraint, because Postgres does not treat `NULL` tenant values as equal for uniqueness) — but
recorded under a distinct audit action (`model.rollback_alias` vs. `model.set_alias`) so intent
is visible in the audit trail without inferring it from the target version. See
`tests/model/test_model_governance.py::test_rollback_repoints_the_alias_and_is_distinguishable_in_the_audit_trail`.

## 3. The adapter itself

`hermes_rpt.models.transformer.adapter.HermesRPTAdapter` — a Houlsby-style residual bottleneck
adapter (down-project → GELU → up-project → residual add onto the backbone's pooled embedding),
followed by its own small classification head. `HermesRPTWithAdapter` wraps a
`HermesRPTBackbone` (Phase 11) plus one `HermesRPTAdapter`:

* Every backbone parameter has `requires_grad = False`, and the backbone's forward pass runs
  inside `torch.no_grad()` besides — belt and suspenders. Verified directly:
  `tests/model/test_adapter_shapes.py::test_training_the_adapter_never_changes_a_single_backbone_weight`
  trains for several steps and asserts every backbone tensor is bit-identical afterward.
* Only `HermesRPTAdapter.state_dict()` is ever serialized to a tenant's checkpoint — the
  backbone's weights are never written alongside it. "Shared models must never contain
  tenant-private adapter weights" holds structurally: the two are always separate files, and the
  training service (`hermes_rpt.models.transformer.adapter_training.
  TenantAdapterTrainingService`) never has a code path that serializes both together.
* Checkpoints land under `checkpoint_dir/<tenant_id>/adapter_<size>.pt` — "Checkpoints must be
  stored in tenant-specific paths."

## 4. Governance enforcement (`hermes_rpt.registry.service.ModelRegistryService`)

* **`register_adapter_candidate(adapter, *, base_model, tenant_context)`** — `tenant_context` is
  required (not optional, unlike the shared-model-capable `register_candidate`): "training jobs
  must include trusted tenant context." Checks `_ensure_compatible(base_model, adapter)` before
  persisting, and `TenantModelAdapterRepository.add` (a `TenantScopedRepository`) refuses to
  persist an adapter whose `tenant_id` doesn't match the caller's — "a tenant adapter belongs to
  exactly one tenant," "Tenant A cannot load Tenant B's adapter." Always forces `stage =
  CANDIDATE`, same "do not promote automatically" rule as `register_candidate`.
* **`transition_stage` / `transition_adapter_stage`** — unchanged state machine (`CANDIDATE →
  STAGING → PRODUCTION`, `→ ARCHIVED` from anywhere but terminal), but promoting *to* `STAGING`
  or `PRODUCTION` now requires `ScopeName.MODEL_PROMOTE` in the caller's `TenantContext.scopes`
  — "promotion requires authorised approval." Archiving/demoting does not require the scope
  (failing safe should never need extra permission). A `None` `tenant_context` means a trusted
  system/CLI caller (the pre-Phase-14 convention `register_candidate` already used) and skips the
  check entirely.
* **`set_alias` / `rollback_alias`** — same approval gate, plus: the target `ModelVersion` must
  be active and not `ARCHIVED`; if it's tenant-private, it must belong to the aliasing tenant; if
  an adapter is included, it must belong to the aliasing tenant (enforced via
  `TenantModelAdapterRepository.require`, which raises `TenantMismatchError` — never leaks
  whether another tenant's adapter exists) and pass `_ensure_compatible` against the base model.
* **`deactivate_model_version` / `deactivate_adapter`** — flips `is_active = False`, audited.
  `ModelVersionRepository.get_production_model` already filters on `is_active`, so a deactivated
  model stops being resolvable immediately, without a stage transition.
* **`IncompatibleAdapterError`** — "incompatible ontology or feature versions must block
  deployment," checked at both adapter registration time and alias-pointing time (an adapter
  compatible with its own base model can still be rejected if someone tries to alias it alongside
  a *different*, incompatible base — see
  `test_pointing_an_alias_at_an_incompatible_adapter_is_rejected`).

## 5. The tenant-adaptation experiment

`make adapt-hermes-rpt` (`apps/trainer/main.py`'s `adapt` command):

1. Provisions synthetic Tenant-Alpha and Tenant-Beta datasets.
2. Trains one shared Hermes-RPT-0.1 (Tiny) backbone+head (`HermesRPTTrainingService`, same as
   Phase 11) — trained under Alpha's `TenantContext` (the training service requires a concrete
   one) then reassigned to `tenant_id = None` before promotion, standing in for a shared/consented
   training pool. Promotes it to `PRODUCTION` and points a shared `"production"` alias at it.
3. Extracts just the backbone's `state_dict()` from that checkpoint (discarding the shared
   classification head) and trains one `HermesRPTAdapter` per tenant on top of it, each on that
   tenant's own data only, via `TenantAdapterTrainingService`. Promotes each to `PRODUCTION` and
   points that tenant's own private `"production"` alias at `(shared base, that tenant's
   adapter)`.
4. **Proves tenant isolation against the real governance layer**, not just a repository unit
   test: Alpha's `TenantContext` attempts to `TenantModelAdapterRepository.get()` Beta's adapter
   (must return `None`) and attempts `set_alias(..., tenant_model_adapter_id=<Beta's adapter>,
   tenant_context=alpha_ctx)` (must raise `TenantMismatchError`). Both are asserted; the run
   fails loudly if either isolation property doesn't hold.
5. Evaluates four configurations on the relevant tenant's own test split and prints one
   comparison table: shared base alone on Alpha, shared base alone on Beta, shared base + Alpha's
   adapter, shared base + Beta's adapter.

## 6. Known gaps / follow-up

* As with every synthetic-data experiment in this project (see
  [BASELINE_MODELS.md](BASELINE_MODELS.md) §10, [HERMES_RPT_PRETRAINING.md](HERMES_RPT_PRETRAINING.md)
  §10), the synthetic delay label is only weakly correlated with available features — read the
  `adapt` command's comparison table as a pipeline-correctness demonstration (governance,
  checkpoint isolation, tenant isolation all wired correctly end to end), not as evidence that
  adapters improve real predictive performance.
* The shared base in this experiment is trained on Alpha's data standing in for a shared/consented
  pool — Phase 12's real cross-tenant consent gate (`hermes_rpt.models.transformer.
  pretraining_consent`) is not re-exercised here; a production shared base would go through that
  gate first.
* No REST endpoint serves an adapter-combined prediction yet — Phase 13's inference API is
  explicitly scoped to baseline models only (see [INFERENCE_API.md](INFERENCE_API.md) §3); wiring
  `ModelAlias` resolution (base + adapter) into a serving endpoint, and recording both
  `model_version_id` and `tenant_model_adapter_id` on the resulting `PredictionResult` (the
  column has existed since Phase 2), is natural follow-up once Hermes-RPT serving exists at all.
* `ModelAlias` uniqueness is enforced in the service layer (query-then-upsert), not a DB
  constraint — documented in `hermes_rpt.registry.models.ModelAlias`'s docstring as a deliberate
  choice (Postgres does not treat `NULL` tenant values as equal for a unique constraint to catch
  duplicates). A concurrent double-`set_alias` race is theoretically possible; the `version`
  optimistic-lock column on `ModelAlias` will raise a `StaleDataError` on the losing writer rather
  than silently corrupting state, but the caller doesn't currently retry on that error.

## 7. Testing

* `tests/model/test_model_governance.py` — compatibility checks, the promotion approval gate
  (with/without `model:promote`, trusted-system-caller bypass, archiving needs no scope), alias
  set/resolve/precedence/rollback, alias target validation (archived/inactive/wrong-tenant
  rejected), deactivation (including cross-tenant rejection for adapters).
* `tests/model/test_registry.py` — unchanged Phase 10 coverage (stage transitions, tenant model
  access isolation) plus adapter-specific candidate-forcing.
* `tests/model/test_adapter_shapes.py` — `HermesRPTAdapter`/`HermesRPTWithAdapter` shape,
  gradient-isolation (backbone frozen — the load-bearing correctness property of this whole
  phase), and numerical-stability tests, no database involved.
* `apps/trainer/main.py`'s `adapt` command is itself an end-to-end test of the full flow against
  real synthetic data — see §5's isolation assertions, which fail the run (not just log a
  warning) if tenant isolation is ever violated.
