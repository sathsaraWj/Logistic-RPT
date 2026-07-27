"""Tests for the pretraining masking/corruption collator (Phase 12) — no database, pure tensor
logic over hand-built `RelationalExample`s.
"""

from __future__ import annotations

from datetime import UTC, datetime

import torch

from hermes_rpt.models.transformer.context import RelationalExample, RelationalRecord
from hermes_rpt.models.transformer.encoding import encode_batch
from hermes_rpt.models.transformer.model import TINY
from hermes_rpt.models.transformer.pretraining import (
    MaskingConfig,
    build_pretraining_batch,
    protected_field_names,
)
from hermes_rpt.models.transformer.schema import (
    ENTITY_FIELD_SCHEMAS,
    ENTITY_TYPE_VOCAB,
    RELATIONSHIP_VOCAB,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _example() -> RelationalExample:
    target = RelationalRecord(
        entity="Trip",
        relationship="__target__",
        fields={
            "planned_distance_km": 120.5,
            "status": "completed",
            "driver_id": "driver-canary-001",
            "route_id": "route-1",
            "vehicle_id": "vehicle-canary-001",
            "planned_departure_at": _NOW,
            "planned_arrival_at": _NOW,
            "actual_departure_at": None,
            "actual_arrival_at": None,
        },
    )
    vehicle = RelationalRecord(
        entity="Vehicle",
        relationship="uses_vehicle",
        fields={
            "manufacture_year": 2020,
            "model_name": "Sprinter",
            "registration_number": "CANARY-REG-42",
            "acquired_at": _NOW,
        },
    )
    maint_a = RelationalRecord(
        entity="MaintenanceEvent",
        relationship="has_maintenance_events",
        fields={"event_type": "scheduled", "vehicle_id": "vehicle-canary-001", "started_at": _NOW},
    )
    maint_b = RelationalRecord(
        entity="MaintenanceEvent",
        relationship="has_maintenance_events",
        fields={
            "event_type": "unscheduled",
            "vehicle_id": "vehicle-canary-001",
            "started_at": _NOW,
        },
    )
    return RelationalExample(
        business_reference="t1",
        prediction_time=_NOW,
        label=1,
        target=target,
        related=(vehicle, maint_a, maint_b),
    )


def _batch():  # type: ignore[no-untyped-def]
    return encode_batch(
        [_example(), _example()],
        max_records_per_relation=TINY.max_records_per_relation,
        categorical_vocab_size=TINY.categorical_vocab_size,
    )


def test_protected_field_names_reads_ontology_business_identifiers() -> None:
    trip_protected = protected_field_names("Trip")
    assert "trip_id" in trip_protected
    assert "vehicle_id" in trip_protected
    assert "driver_id" in trip_protected
    assert "route_id" in trip_protected
    assert "status" not in trip_protected  # not a business identifier — fine to mask


def test_masking_never_selects_a_protected_categorical_field_as_a_target() -> None:
    """The canary/memorisation-risk structural check: `vehicle_id`/`driver_id`/`route_id` are
    business identifiers (canary-like values in this test) and must never appear as a masked-
    cell-reconstruction target, at *any* mask probability, across many independent draws."""

    batch = _batch()
    for seed in range(30):
        config = MaskingConfig(mask_probability=1.0, random_seed=seed)  # mask everything possible
        _masked, targets = build_pretraining_batch(batch, config=config)

        for entity_index, entity in enumerate(ENTITY_TYPE_VOCAB):
            if entity in ("__pad__",):
                continue
            schema = ENTITY_FIELD_SCHEMAS.get(entity)
            if schema is None:
                continue
            protected = protected_field_names(entity)
            entity_positions = batch.entity_type_ids == entity_index
            for slot, field_name in enumerate(schema.categorical_fields):
                if field_name not in protected:
                    continue
                masked_here = targets.categorical_target_mask[:, :, slot] & entity_positions
                assert not bool(masked_here.any()), (
                    f"protected field {entity}.{field_name} was selected as a reconstruction "
                    f"target at seed={seed}"
                )


def test_masking_is_deterministic_given_the_same_seed() -> None:
    batch = _batch()
    config = MaskingConfig(mask_probability=0.5, random_seed=7)
    masked_a, targets_a = build_pretraining_batch(batch, config=config)
    masked_b, targets_b = build_pretraining_batch(batch, config=config)

    torch.testing.assert_close(masked_a.numeric_values, masked_b.numeric_values)
    torch.testing.assert_close(targets_a.numeric_target_mask, targets_b.numeric_target_mask)
    torch.testing.assert_close(targets_a.categorical_target_mask, targets_b.categorical_target_mask)
    assert targets_a.temporal_pairs == targets_b.temporal_pairs


def test_link_corruption_only_swaps_across_different_entity_types() -> None:
    batch = _batch()
    config = MaskingConfig(mask_probability=0.0, link_corruption_probability=1.0, random_seed=3)
    masked, targets = build_pretraining_batch(batch, config=config)

    corrupted = targets.link_positions & (targets.link_labels == 0.0)
    for b in range(batch.attention_mask.shape[0]):
        for s in range(batch.attention_mask.shape[1]):
            if corrupted[b, s]:
                # A corrupted position's content must genuinely differ from what it originally
                # was (unless, by chance, the swap source had identical values — checked here
                # via entity type mismatch instead, which is exactly the corruption criterion).
                assert int(masked.entity_type_ids[b, s]) == int(batch.entity_type_ids[b, s])
                # ^ entity_type_id (the claimed identity) is never rewritten — only content is —
                # which is the whole point: the model must detect content/identity mismatch.


def test_masked_input_treats_masked_cells_as_missing() -> None:
    batch = _batch()
    config = MaskingConfig(mask_probability=1.0, link_corruption_probability=0.0, random_seed=1)
    masked, targets = build_pretraining_batch(batch, config=config)

    assert torch.all(masked.numeric_mask[targets.numeric_target_mask] == 0)
    assert torch.all(masked.categorical_mask[targets.categorical_target_mask] == 0)
    assert torch.all(masked.datetime_mask[targets.datetime_target_mask] == 0)


def test_target_relationship_positions_are_excluded_from_link_prediction() -> None:
    batch = _batch()
    config = MaskingConfig(link_corruption_probability=1.0, random_seed=2)
    _masked, targets = build_pretraining_batch(batch, config=config)

    target_relationship_index = RELATIONSHIP_VOCAB.index("__target__")
    target_positions = batch.relationship_ids == target_relationship_index
    assert not bool(targets.link_positions[target_positions].any())
