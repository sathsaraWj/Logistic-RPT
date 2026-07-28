# Schema Drift Runbook

Status: implements part of Phase 15 of [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md)
("add a schema-drift runbook"). Triggered by `hermes_schema_fingerprint_changes_total` or
`hermes_mapping_suspensions_total` rising for a tenant (see
[INCIDENT_RUNBOOK.md](INCIDENT_RUNBOOK.md) for how you'd land here from an alert).

## 1. What already happened automatically

Schema drift detection and mapping suspension are not manual steps — by the time this runbook
is relevant, the platform has already:

1. Compared the new schema snapshot against the previous one
   (`hermes_rpt.schemas.drift.compare_snapshots`, run automatically at the end of every
   `SchemaDiscoveryService.run_discovery` — see [docs/ARCHITECTURE.md](../ARCHITECTURE.md)).
2. Recorded the diff on the new `SchemaSnapshot.drift_summary` and, if the fingerprint actually
   changed, incremented `hermes_schema_fingerprint_changes_total{tenant_id}`.
3. Auto-suspended (never auto-rewritten) any `ACTIVE` mapping whose source table was affected
   (`MappingService.suspend_affected_by_drift` — sets `SchemaMapping.suspended_due_to_drift =
   true`, audited as `mapping.suspend_due_to_drift`, and increments
   `hermes_mapping_suspensions_total{tenant_id}`).

A suspended mapping is never silently used — `MappingService`'s production-mapping lookup path
raises rather than serving a mapping whose `suspended_due_to_drift` is true. Point-in-time
feature extraction and prediction for that entity fail closed (a `409`-class error at the API,
not a wrong answer) until someone resolves this.

## 2. Diagnose

1. `GET /v1/schema-discovery/{snapshot_id}` — the new snapshot, including `drift_summary.events`
   (each a `DriftEventType`: `table_added`, `table_removed`, `table_renamed_likely`,
   `column_added`, `column_removed`, `column_type_changed`, `column_nullability_changed`,
   `relationship_changed`).
2. `GET /v1/schema-discovery/{snapshot_id}/compare?other_snapshot_id=...` if you need to compare
   against a different pair than "immediately previous."
3. `table_renamed_likely` is a *heuristic flag*, not a confirmed rename
   (`hermes_rpt.schemas.drift`'s `_RENAME_SIMILARITY_THRESHOLD = 0.7`, column-fingerprint
   overlap) — verify with the tenant or by inspecting the actual source schema before treating
   it as one; the platform never auto-approves an inferred rename.
4. Identify every `SchemaMapping` this snapshot suspended: `mapping.suspended_due_to_drift ==
   true` for the affected connection's mappings.

## 3. Resolve

Never "unsuspend" a mapping without addressing what actually changed — `suspended_due_to_drift`
is a correctness gate, not a nuisance flag.

* **Rename**: create a new `MappingVersion` on the affected `SchemaMapping` pointing the source
  reference at the new table/column names, run it through the normal approval flow
  (`submit-for-validation` → `approve` → `activate`), then clear the suspension the same way
  `MappingService.activate` already does — "a freshly activated version supersedes drift"
  (`suspended_due_to_drift` is reset to `false` on activation).
* **Genuine removal** (the source table/column is gone for good, not renamed): the mapping for
  that entity needs to be redesigned or deprecated (`POST /v1/mappings/{id}/deprecate`) —
  loop in the tenant, since this likely means a feature or the whole prediction task can no
  longer be served against their current schema.
* **False positive** (the drift event doesn't actually affect anything this mapping reads): still
  requires a new approved `MappingVersion`/re-activation to clear — there is no "just dismiss
  the drift flag" path, by design (Phase 7 requirement: never resume serving a suspended mapping
  without a real review).

## 4. Verify

1. Confirm the mapping's `state == ACTIVE` and `suspended_due_to_drift == false` after
   activation.
2. Re-run schema discovery once (`POST /v1/schema-discovery`) if there's any doubt the live
   schema still matches what the new mapping version assumes — cheaper than finding out from a
   failed prediction.
3. Confirm `hermes_missing_feature_rate`/`hermes_invalid_data_rate_total` for the affected
   tenant return to their pre-incident baseline on the next dataset build or extraction.
