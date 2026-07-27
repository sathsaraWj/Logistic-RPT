"""Shape, masking, and numerical-stability tests for Hermes-RPT-0.1 (Phase 11) — no database
involved, purely the encoding + model forward/backward pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import torch

from hermes_rpt.models.transformer.context import RelationalExample, RelationalRecord
from hermes_rpt.models.transformer.encoding import (
    DATETIME_FEATURES,
    EncodedBatch,
    encode_batch,
    hash_categorical,
)
from hermes_rpt.models.transformer.model import (
    BASE_EXPERIMENTAL,
    SMALL,
    TINY,
    HermesRPT01,
    HermesRPTConfig,
)
from hermes_rpt.models.transformer.schema import (
    CATEGORICAL_SLOTS,
    DATETIME_SLOTS,
    ENTITY_TYPE_VOCAB,
    NUMERIC_SLOTS,
    RELATIONSHIP_VOCAB,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _target(**overrides: object) -> RelationalRecord:
    fields: dict[str, object] = {
        "planned_distance_km": 100.0,
        "status": "completed",
        "driver_id": "d1",
        "route_id": "r1",
        "vehicle_id": "v1",
        "planned_departure_at": _NOW,
        "planned_arrival_at": _NOW,
        "actual_departure_at": None,
        "actual_arrival_at": None,
    }
    fields.update(overrides)
    return RelationalRecord(entity="Trip", relationship="__target__", fields=fields)


def _related(entity: str, relationship: str, **fields: object) -> RelationalRecord:
    return RelationalRecord(entity=entity, relationship=relationship, fields=fields)


def _example(
    related: tuple[RelationalRecord, ...] = (), *, label: int | None = 1
) -> RelationalExample:
    return RelationalExample(
        business_reference="t1",
        prediction_time=_NOW,
        label=label,
        target=_target(),
        related=related,
    )


def _encode(examples: list[RelationalExample], config: HermesRPTConfig = TINY) -> EncodedBatch:
    return encode_batch(
        examples,
        max_records_per_relation=config.max_records_per_relation,
        categorical_vocab_size=config.categorical_vocab_size,
    )


def test_encoded_batch_has_documented_shapes() -> None:
    batch = _encode([_example(), _example()])
    seq_len = 1 + (len(RELATIONSHIP_VOCAB) - 2) * TINY.max_records_per_relation

    assert batch.numeric_values.shape == (2, seq_len, NUMERIC_SLOTS)
    assert batch.numeric_mask.shape == (2, seq_len, NUMERIC_SLOTS)
    assert batch.categorical_ids.shape == (2, seq_len, CATEGORICAL_SLOTS)
    assert batch.categorical_mask.shape == (2, seq_len, CATEGORICAL_SLOTS)
    assert batch.datetime_values.shape == (2, seq_len, DATETIME_SLOTS, DATETIME_FEATURES)
    assert batch.datetime_mask.shape == (2, seq_len, DATETIME_SLOTS)
    assert batch.entity_type_ids.shape == (2, seq_len)
    assert batch.relationship_ids.shape == (2, seq_len)
    assert batch.attention_mask.shape == (2, seq_len)
    assert batch.labels is not None
    assert batch.labels.shape == (2,)


def test_target_record_always_occupies_position_zero() -> None:
    batch = _encode([_example()])
    assert batch.entity_type_ids[0, 0].item() == ENTITY_TYPE_VOCAB.index("Trip")
    assert batch.relationship_ids[0, 0].item() == RELATIONSHIP_VOCAB.index("__target__")
    assert bool(batch.attention_mask[0, 0])


def test_attention_mask_marks_only_real_records() -> None:
    related = (
        _related("Vehicle", "uses_vehicle", manufacture_year=2020),
        _related("MaintenanceEvent", "has_maintenance_events", event_type="scheduled"),
    )
    batch = _encode([_example(related)])
    # 1 target + 2 related records present; every other slot must be padding (mask False).
    assert int(batch.attention_mask[0].sum().item()) == 3


def test_missing_field_sets_mask_false_and_leaves_zero_value() -> None:
    batch = _encode([_example()])
    # Trip's datetime schema is (planned_departure_at, planned_arrival_at, actual_departure_at,
    # actual_arrival_at) — the last two are None on the target fixture.
    assert bool(batch.datetime_mask[0, 0, 0])  # planned_departure_at present
    assert bool(batch.datetime_mask[0, 0, 1])  # planned_arrival_at present
    assert not bool(batch.datetime_mask[0, 0, 2])  # actual_departure_at missing
    assert not bool(batch.datetime_mask[0, 0, 3])  # actual_arrival_at missing
    assert torch.all(batch.datetime_values[0, 0, 2] == 0.0)
    assert torch.all(batch.datetime_values[0, 0, 3] == 0.0)


def test_related_records_beyond_the_cap_are_truncated() -> None:
    related = tuple(
        _related("MaintenanceEvent", "has_maintenance_events", event_type="scheduled")
        for _ in range(TINY.max_records_per_relation + 5)
    )
    batch = _encode([_example(related)])
    maintenance_count = int(
        (batch.entity_type_ids[0] == ENTITY_TYPE_VOCAB.index("MaintenanceEvent")).sum().item()
    )
    assert maintenance_count == TINY.max_records_per_relation  # capped, not 5-over


def test_hash_categorical_is_deterministic_and_avoids_the_missing_sentinel() -> None:
    a = hash_categorical("vehicle-42", vocab_size=1024)
    b = hash_categorical("vehicle-42", vocab_size=1024)
    assert a == b
    assert 1 <= a < 1024  # 0 is reserved for missing


def test_batch_without_labels_when_any_example_has_none() -> None:
    examples = [_example(label=1), _example(label=None)]
    batch = _encode(examples)
    assert batch.labels is None  # inference-time batches carry no labels at all


@pytest.mark.parametrize("config", [TINY, SMALL, BASE_EXPERIMENTAL])
def test_every_configured_model_size_constructs_and_runs_forward(config: HermesRPTConfig) -> None:
    batch = _encode([_example(), _example()], config)
    model = HermesRPT01(config)
    logits = model(batch)
    assert logits.shape == (2,)
    assert torch.all(torch.isfinite(logits))


def test_forward_and_backward_pass_produce_finite_values() -> None:
    """Numerical-stability check (Phase 11 requirement 11): no NaN/Inf anywhere in the logits,
    the loss, or any parameter's gradient after one backward pass."""

    related = (
        _related("Vehicle", "uses_vehicle", manufacture_year=2019),
        _related("FuelEvent", "has_fuel_events", quantity_litres=40.0, occurred_at=_NOW),
    )
    examples = [_example(related, label=1), _example(label=0)]
    batch = _encode(examples)
    model = HermesRPT01(TINY)
    logits = model(batch)
    assert torch.all(torch.isfinite(logits))

    assert batch.labels is not None
    loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, batch.labels)
    assert torch.isfinite(loss)
    loss.backward()

    for name, param in model.named_parameters():
        if param.grad is not None:
            assert torch.all(torch.isfinite(param.grad)), f"non-finite gradient in {name}"


def test_predict_proba_returns_values_in_unit_interval() -> None:
    batch = _encode([_example(), _example()])
    model = HermesRPT01(TINY)
    proba = model.predict_proba(batch)
    assert torch.all((proba >= 0) & (proba <= 1))


def test_padding_only_positions_do_not_affect_target_pooling() -> None:
    """Two examples with identical target + related records but different amounts of trailing
    padding (achieved by varying the relation-slot budget `K`) must produce the same pooled
    prediction — the transformer's padding mask must make padding invisible to attention, not
    just present-but-zero."""

    related_short = (_related("Vehicle", "uses_vehicle", manufacture_year=2020),)
    torch.manual_seed(0)
    model = HermesRPT01(TINY)
    model.eval()

    wider = HermesRPTConfig(
        name="tiny-wider",
        d_model=TINY.d_model,
        n_heads=TINY.n_heads,
        n_layers=TINY.n_layers,
        ffn_dim=TINY.ffn_dim,
        dropout=TINY.dropout,
        categorical_vocab_size=TINY.categorical_vocab_size,
        max_records_per_relation=TINY.max_records_per_relation + 4,
    )
    batch_a = _encode([_example(related_short)], TINY)
    batch_b = _encode([_example(related_short)], wider)

    with torch.no_grad():
        logits_a = model(batch_a)
        logits_b = model(batch_b)
    torch.testing.assert_close(logits_a, logits_b, atol=1e-5, rtol=1e-4)
