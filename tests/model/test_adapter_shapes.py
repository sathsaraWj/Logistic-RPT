"""Shape, gradient-isolation, and numerical-stability tests for `HermesRPTAdapter` /
`HermesRPTWithAdapter` (Phase 14) — no database involved, purely the model forward/backward pass.
The property under test that matters most here: training a `HermesRPTWithAdapter` must never
change a single backbone parameter, only the adapter's own — "the backbone stays frozen" is a
correctness claim, not just a docstring, and this is where it's actually checked.
"""

from __future__ import annotations

from datetime import UTC, datetime

import torch

from hermes_rpt.models.transformer.adapter import (
    AdapterConfig,
    HermesRPTAdapter,
    HermesRPTWithAdapter,
)
from hermes_rpt.models.transformer.context import RelationalExample, RelationalRecord
from hermes_rpt.models.transformer.encoding import EncodedBatch, encode_batch
from hermes_rpt.models.transformer.model import TINY, HermesRPTBackbone

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


def _example(label: int | None = 1) -> RelationalExample:
    return RelationalExample(
        business_reference="t1", prediction_time=_NOW, label=label, target=_target(), related=()
    )


def _encode(examples: list[RelationalExample]) -> EncodedBatch:
    return encode_batch(
        examples,
        max_records_per_relation=TINY.max_records_per_relation,
        categorical_vocab_size=TINY.categorical_vocab_size,
    )


def _model() -> HermesRPTWithAdapter:
    backbone = HermesRPTBackbone(TINY)
    adapter = HermesRPTAdapter(TINY, AdapterConfig(bottleneck_dim=4))
    return HermesRPTWithAdapter(backbone, adapter)


def test_forward_produces_finite_logits_of_the_right_shape() -> None:
    batch = _encode([_example(), _example()])
    model = _model()
    logits = model(batch)
    assert logits.shape == (2,)
    assert torch.all(torch.isfinite(logits))


def test_predict_proba_returns_values_in_unit_interval() -> None:
    batch = _encode([_example(), _example()])
    model = _model()
    proba = model.predict_proba(batch)
    assert torch.all((proba >= 0) & (proba <= 1))


def test_backbone_parameters_are_frozen() -> None:
    model = _model()
    assert all(not p.requires_grad for p in model.backbone.parameters())
    assert all(p.requires_grad for p in model.adapter.parameters())


def test_training_the_adapter_never_changes_a_single_backbone_weight() -> None:
    torch.manual_seed(0)
    model = _model()
    backbone_before = {name: tensor.clone() for name, tensor in model.backbone.state_dict().items()}

    batch = _encode([_example(label=1), _example(label=0)])
    assert batch.labels is not None
    optimizer = torch.optim.Adam(model.adapter.parameters(), lr=1e-2)

    for _step in range(5):
        optimizer.zero_grad()
        logits = model(batch)
        loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, batch.labels)
        loss.backward()  # type: ignore[no-untyped-call]
        optimizer.step()

    for name, tensor in model.backbone.state_dict().items():
        torch.testing.assert_close(tensor, backbone_before[name])


def test_forward_and_backward_pass_produce_finite_gradients_for_the_adapter_only() -> None:
    batch = _encode([_example(label=1), _example(label=0)])
    model = _model()
    logits = model(batch)
    assert batch.labels is not None
    loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, batch.labels)
    loss.backward()  # type: ignore[no-untyped-call]

    for name, param in model.adapter.named_parameters():
        assert param.grad is not None, f"missing gradient for adapter.{name}"
        assert torch.all(torch.isfinite(param.grad)), f"non-finite gradient in adapter.{name}"
    for _name, param in model.backbone.named_parameters():
        assert param.grad is None


def test_loading_a_pretrained_backbone_state_dict_into_the_adapter_wrapper_works() -> None:
    """The backbone `HermesRPTWithAdapter` wraps is the same `HermesRPTBackbone` class Phase 11
    trains and Phase 12 pretrains — a real trained state dict must load without shape errors."""

    source_backbone = HermesRPTBackbone(TINY)
    adapter = HermesRPTAdapter(TINY, AdapterConfig())
    target_backbone = HermesRPTBackbone(TINY)
    target_backbone.load_state_dict(source_backbone.state_dict())
    model = HermesRPTWithAdapter(target_backbone, adapter)

    batch = _encode([_example()])
    logits = model(batch)
    assert torch.all(torch.isfinite(logits))
