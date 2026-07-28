# Baseline Models

Status: implements Phase 10 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Builds on Phase 9
([DATASET_BUILDING.md](DATASET_BUILDING.md) — this phase trains on a `BuiltDataset`'s splits).
See [adr/0008-baseline-models-before-relational-transformer.md](adr/0008-baseline-models-before-relational-transformer.md).

## 1. Why baselines first

"Before creating the Relational Transformer, implement baseline models for delivery-delay
prediction." Every baseline here is scikit-learn, not torch — the `ml` dependency group's torch
install is reserved for Phase 11's actual relational transformer, so this phase stays usable
(and fast to iterate on) without it in principle, though both are installed together via
`uv sync --group ml` in practice.

## 2. The three baselines

`hermes_rpt.models.baselines` — one class per algorithm, all implementing
`hermes_rpt.models.interface.BaselineModel` (`fit` / `predict_proba` / `feature_importance`):

| Baseline | Estimator | Missing values | Feature importance |
|---|---|---|---|
| `logistic_regression` | `sklearn.linear_model.LogisticRegression` | Median-imputed, then scaled | Coefficients |
| `gradient_boosted_trees` | `sklearn.ensemble.HistGradientBoostingClassifier` | **Native** — never imputed | Not supported by this estimator |
| `mlp` | `sklearn.neural_network.MLPClassifier` (2 hidden layers) | Median-imputed, then scaled | Not supported |

`HistGradientBoostingClassifier`'s native `NaN` handling is a deliberate choice, not a default
picked at random: Phase 8/9's whole design means a real fraction of feature values are
legitimately missing per tenant ("do not assume all customers have every feature"), and
imputing that away would discard exactly the "this tenant doesn't have this data" signal a tree
split can otherwise use directly.

## 3. Metrics — no accuracy

"Because delivery delays may be imbalanced, do not rely on accuracy alone" — accuracy is not
computed anywhere in `hermes_rpt.models.metrics`. `EvaluationMetrics` covers ROC-AUC, PR-AUC,
precision, recall, F1, Brier score, a 10-bin calibration curve, and a confusion matrix at the
selected threshold. **PR-AUC is the model-selection metric** (`hermes_rpt.models.comparison`),
not ROC-AUC, since it is far more sensitive to how well a model ranks the rare positive
(delayed) class under imbalance.

## 4. Threshold selection

`hermes_rpt.models.metrics.select_threshold` — either a fixed value or, by default
(`ThresholdStrategy.MAX_F1`), the threshold on the **validation** split that maximizes F1. The
test split is only ever used for final evaluation at that already-chosen threshold — never for
picking it, which would leak test information into a modeling decision.

## 5. MLflow tracking, artifacts, and the registry

`hermes_rpt.models.training.BaselineTrainingService.train()`:

1. Builds train/val/test matrices (`hermes_rpt.models.dataset_adapter.build_feature_matrix` —
   missing feature → `NaN`, fixed column order from the `FeatureContract`).
2. Fits the configured baseline, selects a threshold on validation, evaluates on test.
3. Opens an MLflow run and logs: **params** (`TrainingConfig` fields — reproducible seeds
   included), **metrics** (every `EvaluationMetrics` field), **tags** (tenant id or `"shared"`,
   dataset checksum + id, mapping version ids, code revision, feature-contract version, ontology
   version — this is "track: tenant ID or synthetic research-dataset ID, dataset version, feature
   version, mapping version, code revision, training configuration, metrics" satisfied in full),
   and the fitted estimator itself via `mlflow.sklearn.log_model` with an inferred **model
   signature**.
4. Computes a SHA-256 **model checksum** over the fitted estimator's own serialized bytes
   (independent of MLflow's internal storage format — the same "deterministic content hash"
   pattern as Phase 5's schema fingerprint and Phase 9's dataset checksum).
5. Registers a `ModelVersion` row (`hermes_rpt.registry`) at `CANDIDATE` — never higher; see §6.

MLflow's sklearn flavor serializes with [skops](https://skops.readthedocs.io/) by default (not
raw `pickle`), and refuses to load a model referencing a type it doesn't recognize as safe. Our
three baselines only ever produce two such types — `numpy.dtype` and
`sklearn.neural_network._stochastic_optimizers.AdamOptimizer` (the MLP's Adam optimizer state)
— both pure data containers with no executable behavior. `training.py` explicitly trusts exactly
those two via `skops_trusted_types` rather than falling back to less-safe `pickle`-based
serialization to route around the check.

**Tracking store**: MLflow's filesystem backend (`file:///...mlruns`) is in maintenance mode as
of MLflow 3.x and refuses to initialize without an explicit opt-out — this project uses a SQLite
database backend (`sqlite:///mlflow.db`) instead, consistent with `docker-compose.yml`'s MLflow
service, which already used `--backend-store-uri sqlite:////mlflow/mlflow.db`.

## 6. Registry stages — no auto-promotion

`hermes_rpt.registry.service.ModelRegistryService`:

```text
CANDIDATE --stage transition--> STAGING --stage transition--> PRODUCTION
   |                                |                              |
   v                                v                              v
ARCHIVED  <---------------------------------------------------------
```

* `register_candidate()` is the only way a `ModelVersion` enters the registry, and it always
  forces `stage = CANDIDATE` regardless of what the caller set — "do not promote a model
  automatically."
* `transition_stage()` enforces the diagram above (`_VALID_TRANSITIONS`); every transition is
  audited (`hermes_rpt.audit`).
* `ARCHIVED` is terminal — no transition out of it.
* `hermes_rpt.registry.repository.ModelVersionRepository` (Phase 2) already implements "shared OR
  mine, never someone else's" for `ModelVersion.tenant_id IS NULL` (shared) vs. set (private) —
  this phase's tests exercise that directly (tenant model access isolation, §8).

## 7. Comparison report

`hermes_rpt.models.comparison.build_comparison_report` — takes every trained baseline's
`TrainingResult`, selects the highest-PR-AUC model, and renders a markdown table
(`ComparisonReport.as_markdown()`) marking the selection. `apps/trainer/main.py`'s `baselines`
command prints this after training all three.

## 8. Testing

* `tests/model/test_baselines_and_metrics.py` — pure logic: reproducibility (same seed + data →
  identical predictions), `HistGradientBoostingClassifier`'s native `NaN` handling vs. the other
  two baselines' imputation, feature importance (present for logistic regression, `None` for the
  others), input-schema validation (a mismatched column count raises `ValueError` — scikit-learn's
  own guarantee, exercised as a platform contract), `build_feature_matrix`'s missing→`NaN`
  behavior and column ordering, threshold selection, metrics, and the comparison report.
* `tests/model/test_registry.py` — stage-transition validity, registry metadata round-tripping,
  and **tenant model access isolation**: a tenant sees shared + its own models but never another
  tenant's; `get_available_for_tenant`/`require_available_for_tenant` behave like every other
  "not found, not forbidden" tenant boundary in this platform; `TenantModelAdapter` access is
  strictly tenant-scoped.
* `tests/model/test_training_service.py` — full `BaselineTrainingService.train()` against a real
  (temp-directory, SQLite-backed) MLflow tracking store: registers a `CANDIDATE` `ModelVersion`,
  produces metrics + a selected threshold, reports feature importance where supported, and — the
  **model serialization** test — the MLflow-stored artifact loads back via
  `mlflow.sklearn.load_model` and produces predictions on the same input.

All of the above run under `uv run pytest tests/model` (`make test-model`), gated behind
`uv sync --group ml` the same way the rest of the `ml`-dependent tooling is.

## 9. Running it

```bash
make install-ml       # once, to pull in torch/mlflow/scikit-learn
make train-baselines   # or: .\tasks.ps1 train-baselines
```

Provisions a small synthetic Tenant-Alpha dataset (`scripts.build_synthetic_dataset.
provision_and_build_dataset` — the same Phase 9 orchestration `make build-synthetic-dataset`
uses, factored out so it isn't duplicated between the two entry points), trains all three
baselines, and prints the comparison report. Artifacts (SQLite fleet data, MLflow's SQLite
tracking store) land under `data/baseline_training_runs/<git-revision>/` — gitignored, per the
same "generated data must be stored outside the source repository" rule Phase 9 established.

## 10. Known gaps / follow-up

* The synthetic generator's delay outcome (`hermes_rpt.synthetic.generator._resolve_trip_outcome`)
  is now risk-weighted rather than a flat probability: `_generate_trips_deliveries_fuel`
  computes, per trip, a distance signal (`planned_distance_km`), a breakdown signal (recent
  unscheduled maintenance in a trailing 180-day window — the same window the real
  `previous_breakdown_count` feature uses), a vehicle-age signal, and incrementally-accumulated
  recent route/driver delay-rate history (last 20 outcomes each); these are combined with fixed
  weights (distance 0.30, breakdown 0.30, route history 0.15, driver history 0.15, age 0.10) into
  a delay probability between `_MIN_DELAY_PROBABILITY` (0.05) and `_MAX_DELAY_PROBABILITY` (0.45),
  plus independent Gaussian noise so identical risk signals don't always produce the same outcome.
  Cancellation is weighted similarly by breakdown/age risk. This was done specifically so a
  Hermes-RPT-0.1-vs-baseline comparison per [ADR-0008](adr/0008-baseline-models-before-relational-transformer.md)
  can be honest — previously the label was statistically independent of every feature, so no
  comparison result (baseline or transformer) could be more than noise. Determinism per
  `(tenant_slug, seed, start, end)` is unaffected; only the *values* generated for a given seed
  changed, not the reproducibility guarantee itself.
* Gradient-boosted trees has no feature-importance signal via this interface (see §2) —
  permutation importance would need held-out data this interface doesn't currently pass through.
* `apps/trainer/main.py`'s `baselines` command always trains against a single freshly-provisioned
  synthetic tenant; there is no CLI option yet to point it at an already-built (e.g. real
  customer) dataset — that wiring is natural follow-up once Phase 13's inference path exists to
  consume the resulting models.
