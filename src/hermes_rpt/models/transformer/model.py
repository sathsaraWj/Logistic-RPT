"""Hermes-RPT-0.1: an experimental relational transformer (Phase 11) — see
docs/HERMES_RPT_0_1.md for the full architecture writeup.

Forward pass shapes: input `EncodedBatch` (`B, S, ...`, `hermes_rpt.models.transformer.encoding`)
-> `RecordEncoder` -> `(B, S, d_model)` -> stacked `nn.TransformerEncoderLayer`s (self-attention
+ FFN, `src_key_padding_mask` derived from `attention_mask`) -> `(B, S, d_model)` -> **target
entity pooling** (index position 0, which `encode_batch` always reserves for the target record)
-> `(B, d_model)` -> classification head -> `(B,)` logits.
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
from torch import nn

from hermes_rpt.models.transformer.encoding import EncodedBatch
from hermes_rpt.models.transformer.modules import RecordEncoder


@dataclass(frozen=True, slots=True)
class HermesRPTConfig:
    name: str
    d_model: int
    n_heads: int
    n_layers: int
    ffn_dim: int
    dropout: float
    categorical_vocab_size: int
    max_records_per_relation: int

    def __post_init__(self) -> None:
        if self.d_model % self.n_heads != 0:
            raise ValueError(
                f"{self.name}: d_model ({self.d_model}) must be divisible by "
                f"n_heads ({self.n_heads})"
            )


# "Only train Tiny initially" — Small and Base-experimental are defined (and construction-tested)
# so the config-driven sizing story is real, not aspirational, but this phase only trains Tiny.
TINY = HermesRPTConfig(
    name="tiny",
    d_model=32,
    n_heads=2,
    n_layers=2,
    ffn_dim=64,
    dropout=0.1,
    categorical_vocab_size=1024,
    max_records_per_relation=4,
)
SMALL = HermesRPTConfig(
    name="small",
    d_model=128,
    n_heads=4,
    n_layers=4,
    ffn_dim=256,
    dropout=0.1,
    categorical_vocab_size=4096,
    max_records_per_relation=8,
)
BASE_EXPERIMENTAL = HermesRPTConfig(
    name="base_experimental",
    d_model=256,
    n_heads=8,
    n_layers=6,
    ffn_dim=1024,
    dropout=0.1,
    categorical_vocab_size=16384,
    max_records_per_relation=16,
)


class HermesRPTBackbone(nn.Module):
    """`RecordEncoder` + the transformer blocks — everything Phase 12's pretraining objectives
    and Phase 11's classification head share. Extracted as its own module specifically so a
    pretrained backbone's weights can be loaded into a fresh `HermesRPT01` afterward
    (`HermesRPT01.load_pretrained_backbone`) — pretraining and fine-tuning use the same backbone
    shape, only the head(s) on top differ."""

    def __init__(self, config: HermesRPTConfig) -> None:
        super().__init__()
        self.config = config
        self.record_encoder = RecordEncoder(config.d_model, config.categorical_vocab_size)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=config.d_model,
            nhead=config.n_heads,
            dim_feedforward=config.ffn_dim,
            dropout=config.dropout,
            activation="gelu",
            batch_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=config.n_layers)

    def forward(self, batch: EncodedBatch) -> torch.Tensor:
        records = self.record_encoder(batch)  # (B, S, d_model)
        # PyTorch's key_padding_mask convention: True means "ignore this position."
        key_padding_mask = ~batch.attention_mask
        encoded: torch.Tensor = self.transformer(
            records, src_key_padding_mask=key_padding_mask
        )  # (B, S, d_model)
        return encoded


class HermesRPT01(nn.Module):
    def __init__(self, config: HermesRPTConfig) -> None:
        super().__init__()
        self.config = config
        self.backbone = HermesRPTBackbone(config)
        self.classification_head = nn.Sequential(
            nn.LayerNorm(config.d_model),
            nn.Linear(config.d_model, config.d_model // 2),
            nn.GELU(),
            nn.Linear(config.d_model // 2, 1),
        )

    def load_pretrained_backbone(self, backbone_state_dict: dict[str, torch.Tensor]) -> None:
        """Loads Phase 12 pretraining weights into this model's backbone only — the
        classification head always starts fresh, since pretraining never trains it."""

        self.backbone.load_state_dict(backbone_state_dict)

    def forward(self, batch: EncodedBatch) -> torch.Tensor:
        encoded = self.backbone(batch)  # (B, S, d_model)
        target_pooled = encoded[:, 0, :]  # target entity pooling — position 0 is always the target
        logits: torch.Tensor = self.classification_head(target_pooled).squeeze(-1)  # (B,)
        return logits

    def predict_proba(self, batch: EncodedBatch) -> torch.Tensor:
        with torch.no_grad():
            return torch.sigmoid(self.forward(batch))
