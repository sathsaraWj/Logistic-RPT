"""Tenant-private adapters for Hermes-RPT-0.1 (Phase 14) — "Shared Hermes-RPT base + tenant-
specific private adapter."

A residual bottleneck adapter (Houlsby-style: down-project, nonlinearity, up-project, residual
add), inserted after the shared backbone's target-entity pooling and *before* a small,
tenant-private classification head. Only the adapter's own parameters are ever trained — the
backbone is frozen (`requires_grad=False` on every backbone parameter, and its forward pass runs
under `torch.no_grad()` besides) — so a shared base model's weights never change, and a tenant's
adapter checkpoint never contains a single backbone weight: "Shared models must never contain
tenant-private adapter weights" holds structurally, not just by convention, because the two are
always serialized separately (`HermesRPTAdapter.state_dict()` vs. `HermesRPTBackbone.
state_dict()` — see `hermes_rpt.models.transformer.adapter_training`).
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
from torch import nn

from hermes_rpt.models.transformer.encoding import EncodedBatch
from hermes_rpt.models.transformer.model import HermesRPTBackbone, HermesRPTConfig


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    bottleneck_dim: int = 8


class HermesRPTAdapter(nn.Module):
    """Bottleneck transform + its own small classification head — self-contained, so a tenant's
    adapter alone (plus a shared, frozen backbone) is enough to produce a prediction; no part of
    the shared classification head Phase 11 trains is reused here."""

    def __init__(self, model_config: HermesRPTConfig, adapter_config: AdapterConfig) -> None:
        super().__init__()
        d_model = model_config.d_model
        self.down = nn.Linear(d_model, adapter_config.bottleneck_dim)
        self.activation = nn.GELU()
        self.up = nn.Linear(adapter_config.bottleneck_dim, d_model)
        self.head = nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, 1))

    def forward(self, pooled: torch.Tensor) -> torch.Tensor:
        adapted = pooled + self.up(self.activation(self.down(pooled)))
        logits: torch.Tensor = self.head(adapted).squeeze(-1)
        return logits


class HermesRPTWithAdapter(nn.Module):
    """A frozen shared `HermesRPTBackbone` plus one tenant's `HermesRPTAdapter`. Constructed
    fresh per tenant per serving/training call — never persisted as a single combined
    checkpoint, so there is nothing to accidentally leak a shared base into a tenant artifact or
    vice versa."""

    def __init__(self, backbone: HermesRPTBackbone, adapter: HermesRPTAdapter) -> None:
        super().__init__()
        self.backbone = backbone
        self.adapter = adapter
        for parameter in self.backbone.parameters():
            parameter.requires_grad = False

    def forward(self, batch: EncodedBatch) -> torch.Tensor:
        self.backbone.eval()
        with torch.no_grad():
            encoded = self.backbone(batch)  # (B, S, d_model)
        target_pooled = encoded[:, 0, :]  # target entity pooling, same convention as HermesRPT01
        logits: torch.Tensor = self.adapter(target_pooled)
        return logits

    def predict_proba(self, batch: EncodedBatch) -> torch.Tensor:
        with torch.no_grad():
            return torch.sigmoid(self.forward(batch))
