"""Pretraining heads, multi-task loss, and the pretraining wrapper model (Phase 12) — sits on
top of `hermes_rpt.models.transformer.model.HermesRPTBackbone`, the same backbone
`HermesRPT01` uses for fine-tuning, so a pretrained backbone's weights transfer directly
(`HermesRPT01.load_pretrained_backbone`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import torch
from torch import nn

from hermes_rpt.models.transformer.encoding import DATETIME_FEATURES, EncodedBatch
from hermes_rpt.models.transformer.model import HermesRPTBackbone, HermesRPTConfig
from hermes_rpt.models.transformer.pretraining import ReconstructionTargets
from hermes_rpt.models.transformer.schema import CATEGORICAL_SLOTS, DATETIME_SLOTS, NUMERIC_SLOTS


@dataclass(frozen=True, slots=True)
class LossWeights:
    """Configurable multi-task loss weights (Phase 12: "add a multi-task loss with configurable
    weights") — an ablation config sets some of these to 0.0 to isolate one objective at a time
    (`hermes_rpt.models.transformer.pretraining_model.ablation_config`)."""

    numeric: float = 1.0
    categorical: float = 1.0
    datetime: float = 1.0
    link: float = 1.0
    temporal_order: float = 1.0


@dataclass(frozen=True, slots=True)
class PretrainingLossComponents:
    total: torch.Tensor
    numeric: float
    categorical: float
    datetime: float
    link: float
    temporal_order: float = field(default=0.0)


class PretrainingHeads(nn.Module):
    def __init__(self, d_model: int, categorical_vocab_size: int) -> None:
        super().__init__()
        self.numeric_head = nn.Linear(d_model, NUMERIC_SLOTS)
        self.categorical_heads = nn.ModuleList(
            [nn.Linear(d_model, categorical_vocab_size) for _ in range(CATEGORICAL_SLOTS)]
        )
        self.datetime_head = nn.Linear(d_model, DATETIME_SLOTS * DATETIME_FEATURES)
        self.link_head = nn.Linear(d_model, 1)
        # A single learned scalar "chronological score" per record (RankNet-style pairwise
        # ranking) — temporal-order prediction trains this so a later record scores higher than
        # an earlier one, never by reusing another objective's head for an unrelated purpose.
        self.temporal_order_head = nn.Linear(d_model, 1)


class HermesRPTForPretraining(nn.Module):
    def __init__(self, config: HermesRPTConfig) -> None:
        super().__init__()
        self.config = config
        self.backbone = HermesRPTBackbone(config)
        self.heads = PretrainingHeads(config.d_model, config.categorical_vocab_size)

    def forward(self, masked_batch: EncodedBatch) -> torch.Tensor:
        encoded: torch.Tensor = self.backbone(masked_batch)  # (B, S, d_model)
        return encoded

    def backbone_state_dict(self) -> dict[str, torch.Tensor]:
        return self.backbone.state_dict()


def compute_pretraining_loss(
    model: HermesRPTForPretraining,
    encoded: torch.Tensor,
    targets: ReconstructionTargets,
    *,
    weights: LossWeights,
) -> PretrainingLossComponents:
    heads = model.heads
    zero = torch.tensor(0.0)
    total = torch.zeros((), device=encoded.device)

    numeric_loss = zero
    if targets.numeric_target_mask.any():
        predicted = heads.numeric_head(encoded)  # (B, S, NUMERIC_SLOTS)
        numeric_terms = []
        for slot in range(predicted.shape[-1]):
            slot_mask = targets.numeric_target_mask[:, :, slot]
            if not slot_mask.any():
                continue
            target_values = targets.numeric_targets[:, :, slot][slot_mask]
            predicted_values = predicted[:, :, slot][slot_mask]
            # Per-slot magnitude scaling: raw numeric fields span wildly different scales (e.g.
            # a manufacture_year ~2020 vs a distance_km ~100) — an un-normalized MSE would be
            # dominated entirely by whichever field happens to have the largest raw magnitude,
            # drowning out every other objective in the multi-task sum. Scale by the batch's own
            # mean absolute magnitude (clamped to at least 1.0) rather than std: std is
            # ill-conditioned for the small per-slot sample counts a tiny pretraining batch
            # produces (0 for a single masked value, which would *amplify* rather than dampen
            # the loss once clamped away from zero) — mean magnitude stays well-behaved even for
            # one sample.
            scale = target_values.abs().mean().clamp(min=1.0)
            numeric_terms.append(
                torch.nn.functional.mse_loss(predicted_values / scale, target_values / scale)
            )
        if numeric_terms:
            numeric_loss = torch.stack(numeric_terms).mean()
            total = total + weights.numeric * numeric_loss

    categorical_loss = zero
    categorical_terms = []
    for slot, head in enumerate(heads.categorical_heads):
        slot_mask = targets.categorical_target_mask[:, :, slot]
        if not slot_mask.any():
            continue
        logits = head(encoded[slot_mask])  # (N, vocab_size)
        target_ids = targets.categorical_targets[:, :, slot][slot_mask]
        categorical_terms.append(torch.nn.functional.cross_entropy(logits, target_ids))
    if categorical_terms:
        categorical_loss = torch.stack(categorical_terms).mean()
        total = total + weights.categorical * categorical_loss

    datetime_loss = zero
    if targets.datetime_target_mask.any():
        predicted_flat = heads.datetime_head(encoded)  # (B, S, DATETIME_SLOTS * DATETIME_FEATURES)
        predicted = predicted_flat.view(*encoded.shape[:2], DATETIME_SLOTS, DATETIME_FEATURES)
        datetime_loss = torch.nn.functional.mse_loss(
            predicted[targets.datetime_target_mask],
            targets.datetime_targets[targets.datetime_target_mask],
        )
        total = total + weights.datetime * datetime_loss

    link_loss = zero
    if targets.link_positions.any():
        link_logits = heads.link_head(encoded).squeeze(-1)  # (B, S)
        link_loss = torch.nn.functional.binary_cross_entropy_with_logits(
            link_logits[targets.link_positions], targets.link_labels[targets.link_positions]
        )
        total = total + weights.link * link_loss

    temporal_loss = zero
    if targets.temporal_pairs:
        scores = heads.temporal_order_head(encoded).squeeze(-1)  # (B, S)
        diffs = []
        pair_labels = []
        for batch_index, pos_a, pos_b, label in targets.temporal_pairs:
            diffs.append(scores[batch_index, pos_b] - scores[batch_index, pos_a])
            pair_labels.append(float(label))
        diff_tensor = torch.stack(diffs)
        label_tensor = torch.tensor(pair_labels, device=encoded.device)
        temporal_loss = torch.nn.functional.binary_cross_entropy_with_logits(
            diff_tensor, label_tensor
        )
        total = total + weights.temporal_order * temporal_loss

    return PretrainingLossComponents(
        total=total,
        numeric=_scalar(numeric_loss),
        categorical=_scalar(categorical_loss),
        datetime=_scalar(datetime_loss),
        link=_scalar(link_loss),
        temporal_order=_scalar(temporal_loss),
    )


def _scalar(value: torch.Tensor | float) -> float:
    return float(value.item()) if torch.is_tensor(value) else float(value)
