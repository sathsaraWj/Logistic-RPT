"""Tiny-overfitting test (Phase 11 requirement 10) — proof the model and training loop are
wired correctly: Hermes-RPT-0.1 (Tiny) must be able to memorize a handful of synthetic examples
almost perfectly. This says nothing about generalization ("do not claim success merely because
training loss falls" — Phase 11 requirement 14); it is a wiring sanity check, not a claim of
model quality, and is documented as such in docs/HERMES_RPT_0_1.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import torch

from hermes_rpt.models.transformer.context import RelationalExample, RelationalRecord
from hermes_rpt.models.transformer.encoding import encode_batch
from hermes_rpt.models.transformer.model import TINY, HermesRPT01

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _synthetic_examples(n: int) -> list[RelationalExample]:
    examples = []
    for i in range(n):
        # A deliberately learnable rule: even i -> label 1 with a "large" distance, odd i ->
        # label 0 with a "small" distance — memorizable by a tiny model in a few dozen steps.
        label = i % 2
        distance = 400.0 if label == 1 else 20.0
        target = RelationalRecord(
            entity="Trip",
            relationship="__target__",
            fields={
                "planned_distance_km": distance,
                "status": "completed",
                "driver_id": f"driver-{i % 3}",
                "route_id": f"route-{i % 2}",
                "vehicle_id": f"vehicle-{i % 4}",
                "planned_departure_at": _NOW + timedelta(hours=i),
                "planned_arrival_at": _NOW + timedelta(hours=i, minutes=30),
                "actual_departure_at": None,
                "actual_arrival_at": None,
            },
        )
        vehicle = RelationalRecord(
            entity="Vehicle",
            relationship="uses_vehicle",
            fields={"manufacture_year": 2020, "model_name": "Sprinter"},
        )
        examples.append(
            RelationalExample(
                business_reference=f"trip-{i}",
                prediction_time=_NOW + timedelta(hours=i),
                label=label,
                target=target,
                related=(vehicle,),
            )
        )
    return examples


def test_tiny_model_overfits_a_small_synthetic_dataset() -> None:
    torch.manual_seed(0)
    examples = _synthetic_examples(16)
    batch = encode_batch(
        examples,
        max_records_per_relation=TINY.max_records_per_relation,
        categorical_vocab_size=TINY.categorical_vocab_size,
    )
    assert batch.labels is not None

    model = HermesRPT01(TINY)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-2)

    model.train()
    final_loss = float("inf")
    for _ in range(200):
        optimizer.zero_grad()
        logits = model(batch)
        loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, batch.labels)
        loss.backward()
        optimizer.step()
        final_loss = loss.item()

    assert final_loss < 0.05, f"Tiny model failed to overfit 16 examples (final loss {final_loss})"

    model.eval()
    with torch.no_grad():
        predicted = (model.predict_proba(batch) >= 0.5).float()
    accuracy = (predicted == batch.labels).float().mean().item()
    assert accuracy == 1.0, "Tiny model should perfectly memorize this small, learnable dataset"
