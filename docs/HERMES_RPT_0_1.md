# Hermes-RPT-0.1

Status: implements Phase 11 of [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). Builds on Phase 9
([DATASET_BUILDING.md](DATASET_BUILDING.md) — row selection/labeling/splitting) and Phase 10
([BASELINE_MODELS.md](BASELINE_MODELS.md) — the baseline this model is compared against).

**Hermes-RPT-0.1 is an experimental relational transformer, not yet a production foundation
model.** It exists to test whether attending directly over a target `Trip`'s raw relational
context (its vehicle, driver history, route history, deliveries, maintenance events, fuel
events) beats Phase 10's tabular baselines, which only ever see pre-aggregated scalar features.

## 1. Architecture

```text
RelationalExample (target + related records, hermes_rpt.models.transformer.context)
        |
        v
encode_batch()  -- hermes_rpt.models.transformer.encoding
        |          fixed-shape tensors: numeric/categorical/datetime values+masks,
        |          entity-type ids, relationship ids, attention mask
        v
RecordEncoder  -- hermes_rpt.models.transformer.modules
        |          per field-kind encoder (+ column embedding + missing-value embedding)
        |          summed with entity-type embedding + relationship embedding
        |          -> one d_model vector per record, (B, S, d_model)
        v
nn.TransformerEncoder (n_layers x nn.TransformerEncoderLayer, self-attention + FFN)
        |          src_key_padding_mask = ~attention_mask
        v
target entity pooling  -- index position 0 (always the target record)
        v
classification head  -- LayerNorm -> Linear -> GELU -> Linear -> 1 logit
```

Every component the phase asks for is a real, separate module:

| Component | Where |
|---|---|
| Numeric feature encoder | `NumericFieldEncoder` |
| Categorical feature encoder | `CategoricalFieldEncoder` |
| Timestamp encoder | `TimestampEncoder` |
| Entity/table type embeddings | `RecordEncoder.entity_type_embedding` |
| Column embeddings | `ColumnEmbeddings` |
| Missing-value embeddings | Each field-kind encoder's own `missing_embedding` (categorical's is embedding index 0 — see `modules.py`) |
| Record encoder | `RecordEncoder` |
| Relationship embeddings | `RecordEncoder.relationship_embedding` |
| Transformer blocks | `nn.TransformerEncoder` in `HermesRPT01` |
| Attention masks | `attention_mask` / `src_key_padding_mask` |
| Target entity pooling | `HermesRPT01.forward`, `encoded[:, 0, :]` |
| Classification head | `HermesRPT01.classification_head` |

## 2. Input representation and tensor shapes

One training example = one target `Trip` + a fixed relational context, built by
`hermes_rpt.models.transformer.context.RelationalContextBuilder` — six explicit relationships
(§3), each capped at `K` records (`HermesRPTConfig.max_records_per_relation`), most-recent-first
(deterministic, never random sampling — requirement 7).

`encode_batch()` (`hermes_rpt.models.transformer.encoding`) lays every example out as a fixed
`S = 1 + 6K` sequence (position 0 = target; then six `K`-slot relation blocks, real records
first within each block, padding after). Every record — regardless of entity type — encodes
into the *same* fixed per-record shape (`hermes_rpt.models.transformer.schema`:
`NUMERIC_SLOTS=2`, `CATEGORICAL_SLOTS=4`, `DATETIME_SLOTS=4`), which is what lets records of
different entity types share one batched tensor:

| Tensor | Shape |
|---|---|
| `numeric_values`, `numeric_mask` | `(B, S, 2)` |
| `categorical_ids`, `categorical_mask` | `(B, S, 4)` |
| `datetime_values` | `(B, S, 4, 3)` (3 = hour-of-day / day-of-week / years-since-epoch fractions) |
| `datetime_mask` | `(B, S, 4)` |
| `entity_type_ids`, `relationship_ids`, `attention_mask` | `(B, S)` |
| `labels` | `(B,)`, or `None` at inference time |

Missing fields are never a zero standing in for a real value — `numeric_mask`/`categorical_ids
== 0`/`datetime_mask` mark them explicitly, and each field-kind encoder substitutes a learned
missing-value embedding instead (requirement 5). Records beyond a relation's `K` cap are simply
not fetched in the first place (`fetch_related_records`'s own `LIMIT`, requirement 6) — capping
happens once, at the query layer, not by discarding rows after the fact.

## 3. Relationship representation

Six explicit relationships (`hermes_rpt.models.transformer.schema.RELATIONSHIP_VOCAB`), each a
distinct, learned relationship embedding — never inferred implicitly from position or entity
type alone:

| Relationship | Join | Point-in-time cutoff |
|---|---|---|
| `uses_vehicle` | `Trip.vehicle_id = Vehicle.vehicle_id` | none (vehicle's own fields are point-in-time by nature) |
| `assigned_driver_history` | `Trip.driver_id` self-join | `planned_departure_at < prediction_time` |
| `follows_route_history` | `Trip.route_id` self-join | `planned_departure_at < prediction_time` |
| `has_deliveries` | `Trip.trip_id = Delivery.trip_id` | none — deliveries are planned as part of the trip itself |
| `has_maintenance_events` | `Trip.vehicle_id = MaintenanceEvent.vehicle_id` | `started_at < prediction_time` |
| `has_fuel_events` | `Trip.vehicle_id = FuelEvent.vehicle_id` | `occurred_at < prediction_time` |

Every fetch reuses Phase 8's exact safety machinery
(`hermes_rpt.features.compiler.fetch_related_records`, added this phase alongside the existing
`fetch_target_row`/`fetch_rows_in_range`) — allowlist checks, parameterized queries, a
`< prediction_time` cutoff wherever a timestamp field exists. There is no second, separate query
path for the relational transformer; only a different *shape* of result (raw per-record rows
instead of Phase 8's aggregated scalar features).

## 4. Training objective

Binary cross-entropy against the same `delivery_delay_label` target Phase 9/10 use — this phase
does not introduce a new label definition, only a new input representation. Full-batch (not
mini-batched) training, appropriate at this phase's tiny synthetic dataset sizes (a few hundred
rows) — not a claim about how a production-scale run would train.

`hermes_rpt.models.transformer.training.HermesRPTTrainingService` reuses Phase 9's
`DatasetBuildService` for row selection/labeling/temporal-splitting (`BuiltDataset.splits`) —
each split row's `(business_reference, prediction_time, label)` triple drives
`RelationalContextBuilder.build_example()`, rather than Phase 8's scalar feature extraction.

## 5. Configuration-driven sizes

`hermes_rpt.models.transformer.model.{TINY, SMALL, BASE_EXPERIMENTAL}`:

| Config | d_model | heads | layers | K (records/relation) | Trained? |
|---|---|---|---|---|---|
| Tiny | 32 | 2 | 2 | 4 | **Yes** |
| Small | 128 | 4 | 4 | 8 | No — constructs and runs forward correctly (tested), not trained |
| Base-experimental | 256 | 8 | 6 | 16 | No |

"Only train Tiny initially" — Small and Base-experimental exist so the config-driven sizing
story is real (`tests/model/test_transformer_shapes.py::
test_every_configured_model_size_constructs_and_runs_forward` runs all three), not aspirational.

## 6. Tenant isolation

Requirements 1-2 ("do not encode tenant identity as a predictive feature," "never combine
relational contexts from different tenants") are structural, the same way every other tenant
boundary in this platform is: `RelationalContextBuilder.build_example()` takes one
`TenantContext`, which gates which mappings resolve and which connection pool is used (Phase
4/7) — there is no code path that can pull two tenants' rows into the same batch. `tenant_id`
itself is never written into a `RelationalRecord.fields` dict — verified directly by
`tests/model/test_transformer_context.py::test_no_related_record_field_ever_carries_a_tenant_id`.

## 7. Testing

* **Shape and masking** (`test_transformer_shapes.py`) — every documented tensor shape; the
  target record always at position 0; only real records marked in `attention_mask`; missing
  fields correctly masked with a zeroed value; records beyond `K` truncated; all three configs
  construct and run; a batch with any unlabeled example carries no labels at all.
* **Numerical stability** (requirement 11) — forward + backward pass produce finite values
  everywhere (logits, loss, every parameter's gradient) on a realistic small batch; a dedicated
  test proves extra all-padding sequence length doesn't change the target's pooled output
  (`test_padding_only_positions_do_not_affect_target_pooling`) — the masking is actually
  correct, not just present.
* **Tiny-overfitting** (requirement 10, `test_transformer_overfit.py`) — Tiny must memorize 16
  synthetic examples with a deliberately learnable rule (final loss < 0.05, 100% train accuracy).
  This is a wiring sanity check, **not evidence of generalization** — see §8.
* **Checkpoint save/load** (requirement 12, `test_transformer_checkpoint.py`) — a saved
  `{config, state_dict}` checkpoint reloads to a model producing bit-identical predictions.
* **Relational context** (`test_transformer_context.py`) — real synthetic Alpha *and* Beta data
  (schema-agnostic, same as every earlier phase's Alpha/Beta tests), point-in-time cutoffs
  respected, no `tenant_id` leakage, missing-target-row fails closed.
* **End-to-end training** (`test_transformer_training.py`) — full `HermesRPTTrainingService.
  train()` against a real (temp-directory, SQLite-backed) MLflow store and the real registry
  service: registers a `CANDIDATE` `ModelVersion`, produces metrics, writes a checkpoint file.

## 8. Comparison against the baseline, and why the comparison itself is honest

`uv run --group ml python -m apps.trainer.main hermes-rpt` (`make train-baselines` trains only
Phase 10's baselines; this additionally trains Hermes-RPT-0.1 Tiny on the *identical*
provisioned dataset/split and folds it into the *same* `hermes_rpt.models.comparison.
build_comparison_report` table, selected by PR-AUC like every other comparison in this
platform).

**Do not claim success merely because training loss falls** (requirement 14) — this phase's own
CLI output prints an explicit reminder not to read a Hermes-RPT-0.1 win as a claim of
production-readiness. Concretely:

* The synthetic generator's delay outcome (`hermes_rpt.synthetic.generator.
  _resolve_trip_outcome`) is only weakly correlated with any of the fields this model (or the
  baselines) can see — see docs/BASELINE_MODELS.md §10. A demo run's metrics hovering near
  chance-level (ROC-AUC ~0.5) for both baselines *and* the transformer is an honest reflection of
  that fact, not a bug and not evidence either model kind is "better" in any way that would
  transfer to real, more-informative production data. For the transformer to actually
  demonstrate an advantage over the baselines would require either richer synthetic correlation
  or a real dataset — this phase's job was to build and validate the architecture and pipeline,
  not to win a benchmark on synthetic labels designed for Phase 9's data-quality demonstration,
  not for Phase 11's modeling demonstration.
* The tiny-overfitting test's 100% memorization of 16 examples is a wiring check, not a
  capability claim (§7) — it deliberately does not appear anywhere in the comparison report.

## 9. Computational cost

Tiny is genuinely tiny: `d_model=32`, 2 layers, 2 heads, `S=25` (`1 + 6*4`) — on the order of
tens of thousands of parameters, trains in seconds on CPU for the synthetic dataset sizes this
phase uses. This says nothing about Small or Base-experimental's cost at real data volumes,
which have not been run.

## 10. Security assumptions

* Everything in §6 (tenant isolation) is a hard platform invariant, not specific to this model.
* `torch.load(..., weights_only=False)` is used for this model's own checkpoint format (it
  stores a plain `HermesRPTConfig` dataclass alongside the state dict, which `weights_only=True`
  would reject) — **only ever loaded from checkpoints this platform itself wrote** (training
  output, MLflow-logged artifacts). This is not a safe deserialization boundary for an
  externally-supplied file; do not add a code path that loads a Hermes-RPT checkpoint from an
  untrusted source without revisiting this. Contrast with Phase 10's baselines, where MLflow's
  `skops`-based sklearn flavor enforces an explicit trusted-types allowlist by default — no
  equivalent guard exists yet for this model's checkpoint format.
* Categorical hashing (`hash_categorical`, SHA-256-based) is a one-way, fixed-vocabulary-size
  hash — it does not reversibly encode business identifiers, but a large enough related-record
  set could in principle let an attentive-enough model correlate hash collisions with real
  identities across records it can already see (records already scoped to one tenant per §6).
  Not a new risk this phase introduces beyond what any embedding of a business identifier
  already implies.

## 11. Difference from a language model

Hermes-RPT-0.1 has no vocabulary, no tokenizer, and no notion of a "sequence of words." Its
"tokens" are database *records* of a small fixed set of entity types, and "position" carries no
sequential meaning (there are no positional embeddings at all — order within a relation block is
purely a recency convention baked into `fetch_related_records`, not something the attention
mechanism is told to treat as ordinal). It is closer in spirit to a set-transformer over a
heterogeneous, explicitly-relational neighborhood than to a causal or masked language model.

## 12. Future pretraining objectives

Phase 12 will add self-supervised objectives (masked cell reconstruction, masked categorical
prediction, numeric-value reconstruction, relationship-link prediction, foreign-key target
prediction, record-context matching, table/column semantic alignment, temporal-order
prediction) on top of this same architecture, with a pretraining data collator that preserves
tenant boundaries and relation structure. See docs/HERMES_RPT_PRETRAINING.md once written.
