# Hermes-RPT Relational Pretraining

Status: implements Phase 12 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Builds directly on
Phase 11 ([HERMES_RPT_0_1.md](HERMES_RPT_0_1.md) — the backbone this phase pretrains).

## 1. Objectives implemented

`hermes_rpt.models.transformer.pretraining` implements three real, separately-testable
self-supervised objectives on top of the *same* `EncodedBatch` shape Phase 11 established —
never a second, parallel data path:

| Objective | Covers (from the phase's full example list) | Mechanism |
|---|---|---|
| Masked cell reconstruction | Masked categorical-value prediction, numeric-value reconstruction | A masked cell is made to look exactly like a *missing* one to the encoder (same machinery Phase 11 already has for genuinely-missing fields); a separate target records the true value. Numeric/datetime → regression head + MSE; categorical → classification head over the hashed vocabulary + cross-entropy. |
| Relationship-link prediction | Foreign-key target prediction (a corrupted link *is* a wrong foreign-key target) | A subset of related-record positions have their content swapped for a *different, cross-entity-type* related record's content, while the position's claimed relationship/entity-type embeddings stay unchanged. A link head predicts genuine (1) vs. corrupted (0). |
| Temporal-order prediction | Temporal-order prediction | For pairs of related records of the same entity type (both with a usable timestamp), a RankNet-style pairwise scorer predicts which happened first. |

**Not implemented, by explicit scope decision** (see `pretraining.py`'s module docstring):

* **Table/column semantic alignment** — this platform maps every tenant's heterogeneous schema
  onto one shared canonical ontology *before* anything reaches this model (Phase 6/7). There is
  no second, differently-shaped ontology within this phase's scope to align against.
* **Record-context matching** — its natural formulation overlaps heavily with relationship-link
  prediction as implemented; a second near-duplicate mechanism wasn't judged worth the added
  complexity this phase.

## 2. Protecting direct identifiers

"Avoid objectives that expose direct identifiers unnecessarily" / "avoid masking protected
fields into memorisation targets": `hermes_rpt.models.transformer.pretraining.
protected_field_names(entity)` reads `OntologyFieldDefinition.is_business_identifier` — the same
Phase 6 metadata that already marks a field sensitive, not a second hand-maintained list that
could drift out of sync. A protected field (`trip_id`, `vehicle_id`, `driver_id`, `route_id`, …)
is **never selected as a masked-cell-reconstruction target**, verified directly and
exhaustively by `tests/model/test_pretraining_masking.py::
test_masking_never_selects_a_protected_categorical_field_as_a_target` across 30 independent
seeds at `mask_probability=1.0` (mask everything maskable). Protected fields may still appear,
unmasked, as ordinary context elsewhere in the sequence — only their use *as a reconstruction
target* is forbidden.

## 3. The pretraining collator

`hermes_rpt.models.transformer.pretraining.build_pretraining_batch`:

* **Applies masks** — per field-kind, respecting §2.
* **Preserves tenant boundaries** — operates entirely within one already-tenant-scoped
  `EncodedBatch` (every swap/pair is drawn from *within* the same example); there is no code
  path that reaches across examples or tenants.
* **Preserves relation structure** — a corrupted link keeps its *claimed* relationship/entity-
  type embedding; only the underlying content is swapped, which is exactly what makes the
  corruption detectable (and meaningful) rather than incoherent.
* **Tracks which values are labels** — `ReconstructionTargets` carries explicit per-cell/per-
  position masks and label tensors for every objective; nothing is inferred implicitly at loss
  time.
* **Avoids masking protected fields into memorisation targets** — §2.
* **Supports variable relational contexts** — reuses Phase 11's fixed `S = 1 + relations * K`
  padded layout and `attention_mask` unchanged; masking never changes sequence length.

## 4. Multi-task loss

`hermes_rpt.models.transformer.pretraining_model.compute_pretraining_loss` — a configurable
weighted sum (`LossWeights`: `numeric`, `categorical`, `datetime`, `link`, `temporal_order`).
Setting any weight to `0.0` removes that objective from the trained total while the component
is still computed and reported (useful for monitoring an ablation run) —
`tests/model/test_pretraining_loss.py::
test_zero_weight_removes_an_objective_from_the_total_but_not_the_component_report`.

**Numeric-scale regression guard**: raw numeric fields span wildly different magnitudes (a
`manufacture_year` ~2020 vs. a `distance_km` ~100) — an un-normalized MSE would be dominated
entirely by whichever field has the largest raw magnitude, drowning out every other objective in
the sum. The numeric reconstruction loss rescales each slot's predictions and targets by the
batch's own mean absolute magnitude (clamped to at least `1.0`) before computing MSE — this was
a real bug caught during development (an un-scaled version produced losses in the billions) and
is now covered by a regression test
(`test_numeric_reconstruction_loss_stays_reasonably_scaled`).

## 5. Ablation configuration

Any `LossWeights` instance with every objective but one at `0.0` isolates that objective —
`tests/model/test_pretraining_loss.py::test_ablation_config_can_isolate_a_single_objective` and
`tests/model/test_pretraining_end_to_end.py::
test_ablation_configuration_runs_with_only_the_link_objective_active` both exercise this
directly, the latter through a full `HermesRPTPretrainingService.pretrain()` run.

## 6. Tenant isolation and shared pretraining

Tenant-isolated by default: `HermesRPTPretrainingService.pretrain()` takes exactly one
`TenantContext` and never combines rows across tenants — the same structural guarantee every
other training/extraction path in this platform provides (Phase 8 §6, Phase 11 §6).

**Shared pretraining requires an explicit approved dataset class and consent record.**
`hermes_rpt.models.transformer.pretraining_consent.require_shared_pretraining_consent` is the
enforced gate: it raises `SharedPretrainingConsentError` for the first tenant among a proposed
shared-pretraining set that lacks a `GRANTED` `DataUsageConsent` row of type
`"shared_pretraining"` (`hermes_rpt.tenants.models.DataUsageConsent` — a Phase 2 table whose
docstring already anticipated this exact use). This is a hard stop, not an advisory check: a
shared pretraining run either has consent from *every* contributing tenant or does not run at
all (`tests/model/test_pretraining_consent.py`). No code path currently *calls* this gate with
more than one tenant — combining multiple tenants' data into one pretraining run is a distinct,
not-yet-built feature; the gate exists and is tested so that when that feature is built, it has
nowhere to go except through this check.

## 7. Memorisation-risk / canary testing

Two complementary checks, since "ensure canaries are not returned through inference APIs" has
both a training-data side and a serving side:

1. **Structural exclusion at training time** — §2's protected-field test *is* the canary test:
   business-identifier-shaped values (this test literally uses `"driver-canary-001"`,
   `"vehicle-canary-001"`, `"CANARY-REG-42"` as field values) are exhaustively verified to never
   become reconstruction targets, across 30 seeds at maximum mask probability.
2. **Structural proof at inference time** — `tests/model/test_pretraining_end_to_end.py::
   test_finetuning_inference_output_never_exposes_more_than_a_risk_score`: no matter what a
   (possibly canary-containing) pretrained backbone learned internally, `HermesRPT01`'s only
   externally callable path (`forward`/`predict_proba`) produces exactly one scalar per example
   — there is no reconstruction head, no per-field output, nothing a canary value could ride out
   through, in the model class that actually serves predictions. The reconstruction heads
   (`PretrainingHeads`) live only on `HermesRPTForPretraining`, a class inference never
   instantiates.

## 8. Lineage and reproducibility

`HermesRPTPretrainingService.pretrain()` logs the same lineage tags every other training path in
this platform uses (tenant id, dataset checksum + id, code revision, ontology version) plus
which objectives/weights were active — a pretraining run is exactly as traceable as a
fine-tuning run, not a special case. Reproducibility is verified directly: the same seed
produces bit-identical backbone weights and identical loss curves
(`tests/model/test_pretraining_end_to_end.py::test_pretraining_is_reproducible_given_the_same_seed`).

## 9. Backbone transfer

`hermes_rpt.models.transformer.model.HermesRPTBackbone` is the shared module both
`HermesRPT01` (Phase 11, classification) and `HermesRPTForPretraining` (this phase) wrap —
extracted specifically so `HermesRPTPretrainingService.pretrain()`'s output
(`PretrainingResult.backbone_state_dict`) can be handed straight to
`HermesRPTTrainingService.train(..., pretrained_backbone_state_dict=...)`. The classification
head always starts fresh; pretraining never touches it.

## 10. Experimental report: pretrained vs. non-pretrained Tiny

`uv run --group ml python -m apps.trainer.main pretrain` (`make pretrain-hermes-rpt`) trains all
three Phase 10 baselines, pretrains a Tiny backbone, then fine-tunes *two* Tiny models on the
identical provisioned dataset/split — one from the pretrained backbone, one from scratch — and
folds every result into one comparison report.

**Be honest when synthetic data is insufficient to demonstrate generalisation** — this platform's
synthetic generator (`hermes_rpt.synthetic.generator`) optimizes for realistic missingness,
class imbalance, and data-quality-check coverage (Phase 9's job), not for producing a delay
outcome strongly predictable from the available features (see docs/BASELINE_MODELS.md §10 and
docs/HERMES_RPT_0_1.md §8). Concretely, that means:

* A handful of pretraining epochs on a few hundred synthetic rows, with a self-supervised signal
  built from the same weakly-informative fields the supervised task already struggles with, is
  **not** a setting where a large pretrained-vs-scratch gap should be expected, and this report
  does not claim one occurred as evidence pretraining "works" in any generalizable sense.
* What this phase's tests and CLI run *do* honestly demonstrate: the full pretrain → checkpoint
  → backbone-transfer → fine-tune pipeline is wired correctly end to end, is reproducible, keeps
  every tenant/protected-field boundary this platform requires, and produces a real (if modest,
  synthetic-data-limited) comparison artifact — not a claim about pretraining's value on real
  fleet data, which would require a real dataset to actually test.
* **Do not claim success merely because training loss falls** applies here exactly as it does in
  Phase 11 — a pretraining loss curve going down only proves the multi-task objective is
  learnable at all (a wiring sanity check), never that the resulting representation transfers
  usefully to the downstream task at real data scale.

## 11. Running it

```bash
make install-ml           # once
make pretrain-hermes-rpt  # or: .\tasks.ps1 pretrain-hermes-rpt
```

Artifacts (pretraining + both fine-tuning runs' SQLite fleet data and MLflow tracking store)
land under `data/hermes_rpt_pretraining_runs/<git-revision>/` — gitignored, same as every other
phase's generated-data convention.
