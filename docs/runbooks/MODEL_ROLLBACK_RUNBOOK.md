# Model Rollback Runbook

Status: implements part of Phase 15 of [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md)
("add a model-rollback runbook"; Phase 14 required "rollback must be tested" — this is the
operational counterpart to that test coverage). Triggered by `hermes_prediction_requests_total
{status="error"}` spiking, `hermes_model_calibration_drift` or realized `hermes_model_precision`/
`hermes_model_recall` (from `hermes_rpt.monitoring.outcomes`) degrading for a specific
`model_version_id`, or a bad deploy you already know about.

## 1. Two rollback mechanisms — know which one applies

This platform has two independent ways a model stops being served, from
[MODEL_ADAPTATION.md](../MODEL_ADAPTATION.md) / [BASELINE_MODELS.md](../BASELINE_MODELS.md):

1. **Stage transition** (`ModelRegistryService.transition_stage`) — `PRODUCTION → STAGING` is a
   valid transition; the model stops being returned by `ModelVersionRepository.
   get_production_model` (Phase 13's baseline-serving lookup) the moment its stage changes.
2. **Alias repoint** (`ModelRegistryService.rollback_alias`) — for anything resolved via a
   `ModelAlias` (Phase 14's base+adapter combination lookup), point the alias back at the
   previous `model_version_id`/`tenant_model_adapter_id`. This is the *only* mechanism for an
   adapter combination, since adapters aren't staged through `get_production_model` at all.

Check which lookup path actually serves the task in question before picking a mechanism —
using the wrong one is a no-op that looks like it worked.

## 2. Rollback via stage transition (baseline models, Phase 13's serving path)

```python
from hermes_rpt.registry.enums import ModelStage
from hermes_rpt.registry.service import ModelRegistryService

registry = ModelRegistryService(session)
# 1. Demote the bad PRODUCTION version back to STAGING.
await registry.transition_stage(bad_model_version_id, to_stage=ModelStage.STAGING, tenant_context=ctx)
# 2. Promote the known-good previous version back to PRODUCTION.
await registry.transition_stage(good_model_version_id, to_stage=ModelStage.PRODUCTION, tenant_context=ctx)
await session.commit()
```

Both calls require `ScopeName.MODEL_PROMOTE` on `tenant_context` (or `tenant_context=None` for a
trusted system/CLI caller — see `hermes_rpt.registry.service._require_promotion_authorization`).
Each transition is independently audited (`model.transition_stage`); the two-call shape (demote,
then promote) means the audit trail shows exactly what happened and when, not a single opaque
"rollback" event.

**You need to already know the previous `model_version_id`** — read it from the audit trail
(`action="model.transition_stage"`, filter to this task, most recent `PRODUCTION` promotion
before the bad one) or from `ModelVersionRepository.list_available_for_tenant`.

## 3. Rollback via alias (Phase 14 base+adapter combinations)

```python
await registry.rollback_alias(
    task_definition_id=task_definition_id,
    model_version_id=previous_good_model_version_id,
    tenant_model_adapter_id=previous_good_adapter_id,  # or None for shared-base-only
    tenant_context=ctx,
)
await session.commit()
```

Recorded under the `model.rollback_alias` audit action specifically (not `model.set_alias`) so
it's distinguishable from a forward promotion in the trail — see
`hermes_rpt.registry.service.ModelRegistryService.rollback_alias`'s docstring. Resolve what to
roll back *to* the same way as §2: the alias's own prior `(model_version_id,
tenant_model_adapter_id)` pair, from the audit trail.

## 4. Immediate effect on in-flight serving

`hermes_rpt.inference.model_loading.ModelLoader` caches a loaded model in-process, keyed by
`ModelVersion.id` — a different `model_version_id` after rollback is a cache miss, so **the very
next request already gets the rolled-back version**, no process restart needed. What does *not*
change automatically: any request already in flight when you rolled back completes against
whatever it already loaded. If the bad model is actively producing harmful predictions (not just
wrong ones), consider `ModelRegistryService.deactivate_model_version`/`deactivate_adapter` too —
`is_active=False` is checked structurally by every serving-path lookup, a stronger signal than a
stage/alias change alone.

## 5. Verify

1. `GET /v1/monitoring/summary` (or the next few real predictions) shows `model_version` back to
   the expected value in the response body.
2. `hermes_model_version_usage_total{model_version_id=...}` for the bad version stops
   incrementing; the good version's counter resumes.
3. `hermes_prediction_requests_total{status="error"}` returns to baseline.
4. If labeled outcomes are being recorded for this task
   (`POST /v1/monitoring/predictions/{id}/outcome`), watch realized `hermes_model_precision`/
   `hermes_model_recall`/`hermes_model_calibration_drift` over the following labeled batch to
   confirm the rollback actually fixed the regression, not just reverted the version number.

## 6. This is tested, not just documented

`tests/model/test_registry.py`/`tests/model/test_model_governance.py` cover the stage-transition
and alias-rollback mechanics directly (`test_valid_stage_transitions_succeed`,
`test_rollback_repoints_the_alias_and_is_distinguishable_in_the_audit_trail`) — this runbook
describes operating the same code paths those tests exercise, not a separate manual procedure.
