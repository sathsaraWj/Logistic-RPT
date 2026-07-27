"""Encoder building blocks for Hermes-RPT-0.1 (Phase 11).

Every field-kind encoder (numeric/categorical/timestamp) follows the same shape: project the
value into `d_model`, add a per-slot **column embedding** (so slot 0 of a `Trip`'s numeric
fields means something different from slot 0 of a `Vehicle`'s), and substitute a learned
**missing-value embedding** — never a zero pretending to be a real value — whenever the field
wasn't present on the source record. Each field-kind's contributions are summed across slots,
then combined with the record's **entity/table type embedding** and **relationship embedding**
into one per-record vector (`RecordEncoder`) before the transformer ever sees it.

Shapes throughout: `B` = batch size, `S` = sequence length (`1 + relations * K`, see
`hermes_rpt.models.transformer.encoding`), `d` = `d_model`.
"""

from __future__ import annotations

import torch
from torch import nn

from hermes_rpt.models.transformer.encoding import DATETIME_FEATURES, EncodedBatch
from hermes_rpt.models.transformer.schema import (
    CATEGORICAL_SLOTS,
    DATETIME_SLOTS,
    ENTITY_TYPE_VOCAB,
    NUMERIC_SLOTS,
    RELATIONSHIP_VOCAB,
)


class ColumnEmbeddings(nn.Module):
    """One learned embedding per (field-kind, slot-index) pair — the "column embedding"
    component. Slot indices are positions within `hermes_rpt.models.transformer.schema
    .EntityFieldSchema`'s tuples, shared across all entity types (slot 0 of any entity's numeric
    fields shares this embedding), which keeps the parameter count independent of how many
    entity types exist."""

    def __init__(self, d_model: int) -> None:
        super().__init__()
        self.numeric = nn.Parameter(torch.randn(NUMERIC_SLOTS, d_model) * 0.02)
        self.categorical = nn.Parameter(torch.randn(CATEGORICAL_SLOTS, d_model) * 0.02)
        self.datetime = nn.Parameter(torch.randn(DATETIME_SLOTS, d_model) * 0.02)


class NumericFieldEncoder(nn.Module):
    """`value * per-slot direction + column embedding` when present (an FT-Transformer-style
    numeric embedding), or the slot's missing-value embedding when not — never a bare `0.0`
    standing in for "unknown"."""

    def __init__(self, d_model: int) -> None:
        super().__init__()
        self.direction = nn.Parameter(torch.randn(NUMERIC_SLOTS, d_model) * 0.02)
        self.missing_embedding = nn.Parameter(torch.zeros(NUMERIC_SLOTS, d_model))

    def forward(
        self, values: torch.Tensor, mask: torch.Tensor, column_embeddings: torch.Tensor
    ) -> torch.Tensor:
        # values, mask: (B, S, NUMERIC_SLOTS) -> (B, S, d_model)
        present = values.unsqueeze(-1) * self.direction + column_embeddings
        missing = (self.missing_embedding + column_embeddings).expand_as(present)
        combined = torch.where(mask.unsqueeze(-1), present, missing)
        return combined.sum(dim=2)


class CategoricalFieldEncoder(nn.Module):
    """A shared embedding table over hashed values (`hermes_rpt.models.transformer.encoding.
    hash_categorical`) plus the slot's column embedding. Index 0 is reserved for "missing" by
    the encoding step, so `padding_idx=0` here doubles as the categorical missing-value
    embedding — a real, learned vector, not a zero."""

    def __init__(self, d_model: int, vocab_size: int) -> None:
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, d_model, padding_idx=0)

    def forward(
        self, ids: torch.Tensor, mask: torch.Tensor, column_embeddings: torch.Tensor
    ) -> torch.Tensor:
        # ids, mask: (B, S, CATEGORICAL_SLOTS) -> (B, S, d_model)
        del mask  # id 0 (the missing sentinel) already routes to the learned missing embedding
        result: torch.Tensor = (self.embedding(ids) + column_embeddings).sum(dim=2)
        return result


class TimestampEncoder(nn.Module):
    """Projects the fixed 3-feature datetime encoding
    (`hermes_rpt.models.transformer.encoding._encode_datetime`) into `d_model`, with the same
    missing-embedding substitution as `NumericFieldEncoder`."""

    def __init__(self, d_model: int) -> None:
        super().__init__()
        self.proj = nn.Linear(DATETIME_FEATURES, d_model)
        self.missing_embedding = nn.Parameter(torch.zeros(DATETIME_SLOTS, d_model))

    def forward(
        self, values: torch.Tensor, mask: torch.Tensor, column_embeddings: torch.Tensor
    ) -> torch.Tensor:
        # values: (B, S, DATETIME_SLOTS, DATETIME_FEATURES), mask: (B, S, DATETIME_SLOTS)
        present = self.proj(values) + column_embeddings
        missing = (self.missing_embedding + column_embeddings).expand_as(present)
        combined = torch.where(mask.unsqueeze(-1), present, missing)
        return combined.sum(dim=2)


class RecordEncoder(nn.Module):
    """Combines every field-kind's contribution with the record's entity-type embedding
    ("Trip" vs "Vehicle" vs ...) and relationship embedding ("__target__" vs "uses_vehicle" vs
    ...) into one `d_model` vector per record, then mixes it through a small residual MLP.
    Input: an `EncodedBatch` (`B, S, ...`). Output: `(B, S, d_model)` — one vector per record,
    still unordered/unattended (`hermes_rpt.models.transformer.model` runs the transformer
    blocks over this)."""

    def __init__(self, d_model: int, categorical_vocab_size: int) -> None:
        super().__init__()
        self.columns = ColumnEmbeddings(d_model)
        self.numeric = NumericFieldEncoder(d_model)
        self.categorical = CategoricalFieldEncoder(d_model, categorical_vocab_size)
        self.timestamp = TimestampEncoder(d_model)
        self.entity_type_embedding = nn.Embedding(len(ENTITY_TYPE_VOCAB), d_model, padding_idx=0)
        self.relationship_embedding = nn.Embedding(len(RELATIONSHIP_VOCAB), d_model, padding_idx=0)
        self.norm = nn.LayerNorm(d_model)
        self.mlp = nn.Sequential(
            nn.Linear(d_model, d_model * 2), nn.GELU(), nn.Linear(d_model * 2, d_model)
        )

    def forward(self, batch: EncodedBatch) -> torch.Tensor:
        numeric = self.numeric(batch.numeric_values, batch.numeric_mask, self.columns.numeric)
        categorical = self.categorical(
            batch.categorical_ids, batch.categorical_mask, self.columns.categorical
        )
        timestamp = self.timestamp(
            batch.datetime_values, batch.datetime_mask, self.columns.datetime
        )
        entity = self.entity_type_embedding(batch.entity_type_ids)
        relationship = self.relationship_embedding(batch.relationship_ids)

        combined = numeric + categorical + timestamp + entity + relationship
        combined = self.norm(combined)
        result: torch.Tensor = combined + self.mlp(combined)
        return result
