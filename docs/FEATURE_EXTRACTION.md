# Safe Feature Extraction

Status: implements Phase 8 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Builds on Phase 7
([SCHEMA_MAPPING.md](SCHEMA_MAPPING.md) — an active `MappingDocument` per tenant/entity) and Phase 6
([ONTOLOGY.md](ONTOLOGY.md)). See [adr/0008-baseline-models-before-relational-transformer.md](adr/0008-baseline-models-before-relational-transformer.md)
for why this phase produces plain tabular feature batches rather than a graph/relational input
representation.

## 1. What this phase is and is not

Phase 7 defines and governs a mapping *document*. Phase 8 is what compiles an **active** mapping
into a real, parameterized, allowlist-checked query and turns the result into a normalized
feature value — "the model must not generate or execute arbitrary SQL" (Phase 8 requirement).
There is exactly one place that happens: `hermes_rpt.features.compiler`, which implements a
small, fixed set of query "recipes" keyed by `FeatureKind`, never a per-feature SQL fragment.

## 2. The feature contract

`hermes_rpt.features.contract.FeatureContract` is data, not code — a `task_key`, a
`target_entity` (the ontology entity the extraction starts from), and a tuple of `FeatureSpec`.
`DELIVERY_DELAY_RISK_CONTRACT` is the platform's one registered task so far: 14 features for
predicting whether a `Trip` will be delayed, targeting the `Trip` entity.

Every `FeatureSpec` declares a `kind`:

| Kind | Recipe | Example |
|---|---|---|
| `DIRECT_FIELD` | A mapped field straight off the target row | `planned_trip_distance_km` |
| `DERIVED_FROM_TARGET` | A fixed Python function over one or two target fields | `planned_departure_hour` (`hour_of_day(planned_departure_at)`) |
| `RELATED_COUNT` | `COUNT(*)` over a related entity, optional window/filter | `previous_breakdown_count` |
| `RELATED_RATIO` | Fraction of related rows matching a filter, in a window | `driver_late_delivery_ratio` |
| `RELATED_RECENCY_DAYS` | Days since the most recent related row | `days_since_last_maintenance` |
| `RELATED_LATEST_VALUE` | Most recent (or only) value of a related field, optionally post-processed by a `derive` function | `vehicle_age_years` (`age_years_at_prediction` applied to `Vehicle.acquired_at`) |

Only three features (`planned_departure_hour`, `day_of_week`, `planned_trip_distance_km`) are
`required=True` — everything derived from a related entity a tenant might not have mapped is
optional. This is the concrete mechanism behind "do not assume all customers have every
feature."

### Single-hop join scope

Every `RELATED_*` feature joins the target entity to **one** related entity through a single
shared field name present on both mappings (e.g. `vehicle_id` on both `Trip` and
`MaintenanceEvent`, or `trip_id` — Trip's own identity — on both `Trip` and `Delivery`). This is
a deliberate scope boundary for this phase, not an oversight: `driver_late_delivery_ratio` and
`planned_delivery_count` are both **documented single-hop proxies** for metrics that would
naturally need a multi-hop join in the full ontology (delivery-level lateness attributed to a
driver needs `Trip -> Driver` and `Delivery -> Trip`; per-package load weight needs
`Package -> Order -> Delivery -> Trip`). Each proxy's `description`/`leakage_note` says exactly
what was simplified, rather than silently claiming a fidelity the extractor doesn't have.

## 3. Leakage prevention

Every `FeatureSpec` carries a `leakage_risk` (`none` or `mitigated`) and, when mitigated, a
`leakage_note` explaining the concrete mechanism that prevents it. That mechanism is enforced in
exactly one place, `hermes_rpt.features.compiler`:

* Every related-entity query adds `<timestamp column> < :prediction_time` — never `<=` — so a
  row that happened to be recorded *at* the prediction instant is excluded.
* `historical_window_days` (`hermes_rpt.features.cost_guard.enforce_window_limit`, capped at 365
  days) adds a lower bound, `>= prediction_time - window`.
* `loading_start_delay_minutes` (a `DERIVED_FROM_TARGET` feature) returns `0.0` rather than a
  computed value whenever `actual_departure_at` is missing or not strictly before
  `prediction_time` — a second, independent guard at the derive-function level, in case a target
  row somehow carries a future value.

These are tested directly in `tests/unit/test_feature_compiler.py` (e.g. a row inserted *after*
`prediction_time` for a `RELATED_COUNT`/`RELATED_RECENCY_DAYS`/`RELATED_LATEST_VALUE` feature is
verified absent from the result) and `tests/unit/test_feature_derive.py`.

## 4. Pipeline

```text
FeatureContract + TenantContext
        |
        v
MappingResolver.resolve_target()        -- fails closed (TargetMappingUnavailableError)
        |                                   if the target entity has no active, non-drift-
        v                                   suspended mapping for this tenant
fetch_target_row()                       -- one row, by business_reference, only the ontology
        |                                   fields the contract actually needs, allowlist-checked
        v
for each FeatureSpec:
  DIRECT_FIELD / DERIVED_FROM_TARGET  -> read from the target row (+ apply_derive if derived)
  RELATED_*                           -> MappingResolver.resolve() the related entity
                                          (None -> feature recorded as missing, not an error)
                                          -> compile_and_run_related_feature()
                                          -> apply_derive() if the FeatureSpec sets one
        |
        v
normalize()                              -- data_type coercion + missing_value_behavior
        |
        v
FeatureBatch { features, FeatureLineageRecord }
```

`FeatureExtractionService.extract()` is the only method that touches a customer database.
`FeatureExtractionService.plan()` (dry-run) resolves mappings and reports, per feature, whether
it would be available and from which table/columns — useful for tenant onboarding UIs — but
never opens a connection or resolves a secret (`hermes_rpt.features.planner`).

## 5. Lineage

Every extraction records a `FeatureLineageRecord`: tenant, task key, feature-contract version,
the target mapping's *version id* (not just the mapping id — pins the exact document used),
schema snapshot id, the related-entity mapping version ids actually used, an extraction
timestamp, and the list of features that came back missing and why. This is what lets a later
audit answer "what mapping version produced this training row" even after the mapping has since
changed.

## 6. Endpoints

`apps/api/routers/features.py`, behind `prediction:execute`:

* `GET /v1/features/{task_key}/plan` — dry-run query plan.
* `POST /v1/features/{task_key}/extract` — real extraction for one `business_reference` /
  `prediction_time`.

These exist so the extraction layer is independently reachable and auditable; the actual
prediction-serving endpoint (`POST /v1/predictions/...`, which calls extraction internally and
then runs a model) is Phase 13's job.

## 7. Testing

* `tests/unit/test_feature_contract.py`, `test_feature_derive.py`, `test_feature_normalizer.py`
  — pure-logic tests, no I/O.
* `tests/unit/test_feature_compiler.py` — real `sqlite+aiosqlite` engine, hand-created tables;
  covers point-in-time correctness for every `FeatureKind`.
* `tests/security/test_feature_extraction_service.py` — full pipeline (connection -> mapping ->
  resolver -> compiler -> normalizer -> lineage) against **both Tenant-Alpha-shaped and
  Tenant-Beta-shaped schemas** using `tests.fakes.SQLiteConnector` (a real, in-memory-SQLite-backed
  `DatabaseConnector` — a placeholder fake can't run the real SQL this phase's compiler produces).
  Also covers: a related entity with no active mapping comes back `missing` rather than raising;
  a missing target mapping fails closed; `plan()` never touches the database.

## 8. Follow-up / known gaps

* No multi-hop join support — see §2. A future phase could add it once there's a concrete need
  beyond the two documented proxies.
* `historical_window_days` and `related_row_limit()` (`hermes_rpt.features.cost_guard`) are
  fixed platform-wide constants, not yet tenant- or feature-configurable.
* Integration tests against real Postgres (rather than SQLite) for this phase specifically were
  not added — Docker was not available in this environment; the existing `tests/integration`
  skip-gate pattern (`pytest.skip(... run \`make up\` first)`) applies here too, and the SQLite
  end-to-end tests already exercise the full pipeline's *logic*, just not the Postgres driver.
