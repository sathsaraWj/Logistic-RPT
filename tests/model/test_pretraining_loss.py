"""Tests for the pretraining multi-task loss (Phase 12) — pure tensor logic."""

from __future__ import annotations

from datetime import UTC, datetime

import torch

from hermes_rpt.models.transformer.context import RelationalExample, RelationalRecord
from hermes_rpt.models.transformer.encoding import encode_batch
from hermes_rpt.models.transformer.model import TINY
from hermes_rpt.models.transformer.pretraining import MaskingConfig, build_pretraining_batch
from hermes_rpt.models.transformer.pretraining_model import (
    HermesRPTForPretraining,
    LossWeights,
    compute_pretraining_loss,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _examples() -> list[RelationalExample]:
    target = RelationalRecord(
        entity="Trip",
        relationship="__target__",
        fields={
            "planned_distance_km": 120.5,
            "status": "completed",
            "driver_id": "d1",
            "route_id": "r1",
            "vehicle_id": "v1",
            "planned_departure_at": _NOW,
            "planned_arrival_at": _NOW,
        },
    )
    vehicle = RelationalRecord(
        entity="Vehicle",
        relationship="uses_vehicle",
        fields={"manufacture_year": 2020, "model_name": "Sprinter", "acquired_at": _NOW},
    )
    maint_a = RelationalRecord(
        entity="MaintenanceEvent",
        relationship="has_maintenance_events",
        fields={"event_type": "scheduled", "vehicle_id": "v1", "started_at": _NOW},
    )
    maint_b = RelationalRecord(
        entity="MaintenanceEvent",
        relationship="has_maintenance_events",
        fields={"event_type": "unscheduled", "vehicle_id": "v1", "started_at": _NOW},
    )
    return [
        RelationalExample(
            business_reference="t1",
            prediction_time=_NOW,
            label=1,
            target=target,
            related=(vehicle, maint_a, maint_b),
        ),
        RelationalExample(
            business_reference="t2",
            prediction_time=_NOW,
            label=0,
            target=target,
            related=(vehicle,),
        ),
    ]


def _batch():  # type: ignore[no-untyped-def]
    return encode_batch(
        _examples(),
        max_records_per_relation=TINY.max_records_per_relation,
        categorical_vocab_size=TINY.categorical_vocab_size,
    )


def test_pretraining_loss_is_finite_and_differentiable() -> None:
    torch.manual_seed(0)
    batch = _batch()
    masked_batch, targets = build_pretraining_batch(
        batch, config=MaskingConfig(mask_probability=0.4, random_seed=1)
    )
    model = HermesRPTForPretraining(TINY)
    encoded = model(masked_batch)
    loss = compute_pretraining_loss(model, encoded, targets, weights=LossWeights())

    assert torch.isfinite(loss.total)
    loss.total.backward()  # type: ignore[no-untyped-call]
    for name, param in model.named_parameters():
        if param.grad is not None:
            assert torch.all(torch.isfinite(param.grad)), f"non-finite gradient in {name}"


def test_numeric_reconstruction_loss_stays_reasonably_scaled() -> None:
    """Regression guard: an un-normalized numeric reconstruction loss on a field like
    `manufacture_year` (~2020) would dwarf every other objective — see
    hermes_rpt.models.transformer.pretraining_model's per-slot magnitude scaling."""

    torch.manual_seed(0)
    batch = _batch()
    masked_batch, targets = build_pretraining_batch(
        batch, config=MaskingConfig(mask_probability=1.0, random_seed=2)
    )
    model = HermesRPTForPretraining(TINY)
    encoded = model(masked_batch)
    loss = compute_pretraining_loss(model, encoded, targets, weights=LossWeights())
    assert loss.numeric < 1000.0  # an untrained model's residual should not be astronomical


def test_zero_weight_removes_an_objective_from_the_total_but_not_the_component_report() -> None:
    torch.manual_seed(0)
    batch = _batch()
    masked_batch, targets = build_pretraining_batch(
        batch,
        config=MaskingConfig(mask_probability=0.4, link_corruption_probability=1.0, random_seed=1),
    )
    model = HermesRPTForPretraining(TINY)
    encoded = model(masked_batch)

    full_weights = LossWeights()
    zero_link_weights = LossWeights(link=0.0)

    loss_full = compute_pretraining_loss(model, encoded, targets, weights=full_weights)
    loss_no_link = compute_pretraining_loss(model, encoded, targets, weights=zero_link_weights)

    # The component itself is still computed/reported (useful for monitoring an ablation run)...
    assert loss_no_link.link == loss_full.link
    # ...but it no longer contributes to the trained total.
    assert loss_no_link.total.item() < loss_full.total.item()


def test_ablation_config_can_isolate_a_single_objective() -> None:
    """Ablation configuration (Phase 12 requirement): a weights config with every other
    objective at 0.0 trains only the named objective."""

    torch.manual_seed(0)
    batch = _batch()
    masked_batch, targets = build_pretraining_batch(
        batch, config=MaskingConfig(mask_probability=0.4, random_seed=1)
    )
    model = HermesRPTForPretraining(TINY)
    encoded = model(masked_batch)

    only_categorical = LossWeights(
        numeric=0.0, categorical=1.0, datetime=0.0, link=0.0, temporal_order=0.0
    )
    loss = compute_pretraining_loss(model, encoded, targets, weights=only_categorical)
    assert torch.isclose(loss.total, torch.tensor(loss.categorical), atol=1e-4)
