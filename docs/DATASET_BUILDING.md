# Dataset Building and Data Quality

Status: implements Phase 9 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Builds on Phase 8
([FEATURE_EXTRACTION.md](FEATURE_EXTRACTION.md) — this is what actually runs per row) and reuses
Phase 7's mapping lifecycle and Phase 6's ontology.

## 1. What a dataset build does

`hermes_rpt.datasets.builder.DatasetBuildService.build()`:

```text
DatasetDefinition (task, tenant, time range, label function, split strategy)
        |
        v
resolve the target entity's active mapping (fails closed if none — same as Phase 8)
        |
        v
fetch_rows_in_range()  -- enumerate candidate Trip rows in [start, end), allowlist-checked,
        |                  fetching identity + label-relevant fields directly (never through
        |                  FeatureContract, so a label field can't also leak in as a feature)
        v
for each row: compute_label()  -- hermes_rpt.datasets.label, a fixed function registry
        |        -> None means "outcome not knowable yet" (row excluded, not labeled 0)
        v
point-in-time check: label event must be strictly after prediction_time
        |        -> failure means "excluded", never a crash — see §4
        v
for each labelable row: FeatureExtractionService.extract()  -- Phase 8, per row
        v
quality checks (raw rows + assembled labels) -> DataQualityReport
        v
split_temporally()  -- rows already time-ordered; cut by fraction, no shuffling
        v
statistics + checksum -> DatasetManifest
```

## 2. Point-in-time correctness

Two independent layers, both already covered by tests:

1. **Feature-level** (Phase 8): every feature query cuts at `< prediction_time`. Unchanged here.
2. **Label-level** (this phase): `hermes_rpt.datasets.label.delivery_delay_label` returns `None`
   — not a label — for any trip whose outcome isn't known yet (`planned`/`in_progress`, or a
   `completed` trip missing its arrival timestamp). The builder additionally verifies the label
   event is *strictly after* `prediction_time` before trusting a computed label
   (`DatasetBuildService._is_point_in_time_valid`); if a row's own timestamps are contradictory
   (a real-world data-quality problem — see `check_invalid_date_ordering`), the label is dropped
   rather than trusted, and the build continues rather than failing outright over one bad row.

`prediction_time` for every row equals the target entity's own planning-time field
(`Trip.planned_departure_at`) — the same convention Phase 8's tests and docs use.

## 3. Data-quality checks

`hermes_rpt.datasets.quality` — small, pure, unit-tested functions, each producing an optional
`QualityIssue` (`info` / `warning` / `error` severity). `DatasetBuildService.build()` runs:

| Check | What it catches |
|---|---|
| `check_duplicate_ids` | Duplicate target-entity business IDs |
| `check_invalid_date_ordering` | `actual_arrival_at` before `planned_departure_at` |
| `check_negative_numeric` | Negative `planned_distance_km` |
| `check_unrecognised_values` | A `status` outside the ontology's enum values |
| `check_future_timestamps` | `planned_departure_at` after wall-clock now |
| `check_missing_labels` | Rows with no computable label (info, not an error — expected) |
| `check_class_imbalance` | Minority class below a threshold fraction |
| `check_cross_tenant_contamination` | Any assembled row not belonging to the expected tenant |

`DataQualityReport.passed` is `False` if any `error`-severity issue exists — the report is
produced either way (the build never aborts on a quality finding); a caller decides what to do
with a failed report. This is deliberate: "run it and produce a safe sample data-quality
report" describes an artifact to inspect, not an automatic gate.

## 4. Reproducibility: manifest, lineage, checksum

`hermes_rpt.datasets.manifest.DatasetManifest` — the one artifact worth keeping. Contains **no**
feature value, label, or raw customer data — only counts, a checksum, and identifiers:

* `DatasetLineage`: ontology version, feature-contract version, task key, and — critically — the
  exact `MappingVersion` id and `SchemaSnapshot` id used, not just "the current mapping." Since
  mappings are immutable after activation (Phase 7), this id alone is enough to reconstruct
  exactly what a dataset was built from, even after the tenant's mapping has since changed.
* `checksum` (`compute_dataset_checksum`): a deterministic SHA-256 over every row's feature
  values and label, in row order. Two builds of the same definition against unchanged source
  data produce an identical checksum — verified directly in
  `tests/security/test_dataset_build_service.py::test_checksum_is_reproducible_across_two_builds_of_the_same_data`.
* `feature_statistics` / `label_statistics`: aggregate mean/std/min/max and class counts —
  useful for a training report (Phase 10) without ever re-reading raw rows.

## 5. Temporal splitting

`hermes_rpt.datasets.split.split_temporally` — rows are **not** shuffled. They arrive already
ordered by `prediction_time` (from `fetch_rows_in_range`'s own `ORDER BY`), and the split simply
cuts by fraction: every `train` row's `prediction_time` precedes every `validation` row's, which
precedes every `test` row's. This is "use temporal splits rather than random splits for delay
prediction" — a model trained this way is evaluated on strictly later data than it trained on,
matching how it will actually be used.

## 6. Synthetic data generators

`hermes_rpt.synthetic` — deterministic (seeded), covers all seven entities the
delivery-delay-risk contract touches (`Vehicle`, `Trip`, `MaintenanceEvent`, `OdometerReading`,
`FuelEvent`, `RouteStop`, `Delivery`), for **two differently-shaped tenant schemas**:

| Ontology field (Trip) | Tenant Alpha | Tenant Beta |
|---|---|---|
| table | `trip` | `jobs` |
| `trip_id` | `trip_id` | `job_ref` |
| `vehicle_id` | `vehicle_id` | `asset_ref` |
| `planned_departure_at` | `planned_departure_at` | `sched_depart` |
| `status` | `status` | `job_status` |

(`hermes_rpt.synthetic.schemas.ALL_ENTITY_SCHEMAS` has the full table for all seven entities.)

Realism built in: class imbalance (~85-90% on-time), missing values (odometer reading `source`
is sometimes null, in-progress maintenance has no `completed_at`), vehicle/route histories
(weekly odometer readings, 2-5 maintenance events per vehicle), and trips near the end of the
requested time range that are deliberately left `planned`/unresolved — exactly the "avoid placing
future events into earlier training rows" case, since these have no label yet.

`hermes_rpt.synthetic.generator._inject_defects` additionally seeds a small, fixed set of bad
rows — a duplicate vehicle ID, an arrival before its own departure, a negative distance, a
negative fuel quantity, an unrecognised status, and (when the caller's `end` extends past
wall-clock now) a future-dated trip — so `make build-synthetic-dataset` demonstrates the quality
checks catching real problems, not a hypothetical.

## 7. Running it

```bash
make build-synthetic-dataset        # or: .\tasks.ps1 build-synthetic-dataset
```

Generates Alpha and Beta fleet data, builds a `delivery-delay-risk` dataset for each through the
real service layer (real `ConnectionLifecycleManager`, `MappingService`, `DatasetBuildService` —
only the database driver is SQLite instead of Postgres), and writes
`data/synthetic_datasets/<run_id>/{alpha,beta}-manifest.json` plus a per-tenant SQLite file with
the raw generated rows — all under `data/`, which is gitignored ("generated data must be stored
outside the source repository"). Prints a safe, aggregate report to stdout: row counts, label
balance, checksum, every quality issue found, and per-feature mean/missing-count — never a raw
row.

## 8. Known gaps / follow-up

* Quality checks are reporting-only — a build with `error`-severity issues still produces a
  dataset; nothing here automatically drops or excludes flagged *feature-level* rows (label-level
  and point-in-time issues *are* excluded, per §2). Auto-remediation policy is future work.
* `check_duplicate_ids` and friends only run against the target entity's (`Trip`) raw rows, not
  against related entities (`Vehicle`, `MaintenanceEvent`, ...) — a duplicate business ID on a
  related entity isn't directly flagged by this phase's checks.
* `DatasetManifest` is a file artifact (JSON), not a control-plane database table — there is no
  registry/query API over past dataset builds yet.
* No real-Postgres integration test for this phase specifically, for the same reason as Phase 8
  (Docker unavailable in this environment) — `tests/security/test_dataset_build_service.py`
  exercises the full pipeline's logic against SQLite instead.
