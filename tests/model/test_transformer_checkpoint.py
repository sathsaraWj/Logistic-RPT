"""Checkpoint save/load tests (Phase 11 requirement 12)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import torch

from hermes_rpt.models.transformer.context import RelationalExample, RelationalRecord
from hermes_rpt.models.transformer.encoding import encode_batch
from hermes_rpt.models.transformer.model import TINY, HermesRPT01

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _example() -> RelationalExample:
    target = RelationalRecord(
        entity="Trip",
        relationship="__target__",
        fields={"planned_distance_km": 100.0, "status": "completed", "vehicle_id": "v1"},
    )
    return RelationalExample(
        business_reference="t1", prediction_time=_NOW, label=1, target=target, related=()
    )


def test_checkpoint_round_trip_preserves_predictions(tmp_path: Path) -> None:
    torch.manual_seed(0)
    model = HermesRPT01(TINY)
    model.eval()

    batch = encode_batch(
        [_example(), _example()],
        max_records_per_relation=TINY.max_records_per_relation,
        categorical_vocab_size=TINY.categorical_vocab_size,
    )
    with torch.no_grad():
        original_logits = model(batch)

    checkpoint_path = tmp_path / "hermes_rpt_tiny.pt"
    torch.save({"config": TINY, "state_dict": model.state_dict()}, checkpoint_path)

    checkpoint = torch.load(checkpoint_path, weights_only=False)
    reloaded_model = HermesRPT01(checkpoint["config"])
    reloaded_model.load_state_dict(checkpoint["state_dict"])
    reloaded_model.eval()

    with torch.no_grad():
        reloaded_logits = reloaded_model(batch)

    torch.testing.assert_close(original_logits, reloaded_logits)


def test_checkpoint_file_is_self_describing_about_model_size(tmp_path: Path) -> None:
    model = HermesRPT01(TINY)
    checkpoint_path = tmp_path / "checkpoint.pt"
    torch.save({"config": TINY, "state_dict": model.state_dict()}, checkpoint_path)

    checkpoint = torch.load(checkpoint_path, weights_only=False)
    assert checkpoint["config"].name == "tiny"
    assert checkpoint["config"].d_model == TINY.d_model
