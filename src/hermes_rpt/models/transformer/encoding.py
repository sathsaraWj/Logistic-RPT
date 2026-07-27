"""Turns a batch of `RelationalExample`s into fixed-shape tensors (Phase 11).

Every example is padded to the same sequence length `S = 1 + len(relations) * K` (`K` =
`max_records_per_relation`) — one slot for the target record, then a fixed `K`-slot block per
relation type, real records first within each block and padding after. This is what "support
variable numbers of related records" means concretely: the *slot count* is fixed (so a batch is
a plain rectangular tensor), but how many of those slots hold a real record varies per example,
tracked entirely by `attention_mask`. "Cap relational context sizes" is `K` itself — a hard
`LIMIT` already enforced upstream by `hermes_rpt.features.compiler.fetch_related_records`, not
re-implemented here.

Hashing (categorical fields, including business identifiers) uses SHA-256, not Python's built-in
`hash()` — stable across processes/runs regardless of `PYTHONHASHSEED`, which "deterministic
sampling for reproducible evaluation" (Phase 11) requires.

Tensor shapes (see docs/HERMES_RPT_0_1.md §2 for the authoritative reference):

* `numeric_values`, `numeric_mask`: `(B, S, NUMERIC_SLOTS)`
* `categorical_ids`, `categorical_mask`: `(B, S, CATEGORICAL_SLOTS)`
* `datetime_values`: `(B, S, DATETIME_SLOTS, DATETIME_FEATURES)`
* `datetime_mask`: `(B, S, DATETIME_SLOTS)`
* `entity_type_ids`, `relationship_ids`, `attention_mask`: `(B, S)`
* `labels`: `(B,)` or `None` at inference time
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import torch

from hermes_rpt.models.transformer.context import RelationalExample, RelationalRecord
from hermes_rpt.models.transformer.schema import (
    CATEGORICAL_SLOTS,
    DATETIME_SLOTS,
    ENTITY_FIELD_SCHEMAS,
    ENTITY_TYPE_VOCAB,
    NUMERIC_SLOTS,
    RELATIONSHIP_VOCAB,
)

DATETIME_FEATURES = 3  # [hour_of_day_frac, day_of_week_frac, years_since_epoch_frac]
_DATETIME_EPOCH = datetime(2020, 1, 1, tzinfo=None)
_DATETIME_HORIZON_YEARS = 10.0

_ENTITY_TYPE_INDEX = {name: i for i, name in enumerate(ENTITY_TYPE_VOCAB)}
_RELATIONSHIP_INDEX = {name: i for i, name in enumerate(RELATIONSHIP_VOCAB)}
# Ordered relation slots this encoder lays out per example — every relationship in the vocab
# except the two structural placeholders, in a fixed order so every example's tensor layout
# means the same thing at the same position.
_RELATION_ORDER: tuple[str, ...] = tuple(
    name for name in RELATIONSHIP_VOCAB if name not in ("__pad__", "__target__")
)


@dataclass(frozen=True, slots=True)
class EncodedBatch:
    numeric_values: torch.Tensor
    numeric_mask: torch.Tensor
    categorical_ids: torch.Tensor
    categorical_mask: torch.Tensor
    datetime_values: torch.Tensor
    datetime_mask: torch.Tensor
    entity_type_ids: torch.Tensor
    relationship_ids: torch.Tensor
    attention_mask: torch.Tensor
    labels: torch.Tensor | None

    def to(self, device: torch.device) -> EncodedBatch:
        return EncodedBatch(
            numeric_values=self.numeric_values.to(device),
            numeric_mask=self.numeric_mask.to(device),
            categorical_ids=self.categorical_ids.to(device),
            categorical_mask=self.categorical_mask.to(device),
            datetime_values=self.datetime_values.to(device),
            datetime_mask=self.datetime_mask.to(device),
            entity_type_ids=self.entity_type_ids.to(device),
            relationship_ids=self.relationship_ids.to(device),
            attention_mask=self.attention_mask.to(device),
            labels=self.labels.to(device) if self.labels is not None else None,
        )


def hash_categorical(value: str, *, vocab_size: int) -> int:
    """A stable (not `PYTHONHASHSEED`-dependent) hash into `[1, vocab_size)` — index 0 is
    reserved for "missing/padding" (`CategoricalFieldEncoder`), so a real value never collides
    with it."""

    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return 1 + (int.from_bytes(digest[:8], "big") % (vocab_size - 1))


def _encode_datetime(value: datetime) -> tuple[float, float, float]:
    naive = value.replace(tzinfo=None)
    hour_frac = naive.hour / 24.0
    day_of_week_frac = naive.weekday() / 7.0
    years_since_epoch = (naive - _DATETIME_EPOCH).days / 365.25
    years_frac = max(0.0, min(1.0, years_since_epoch / _DATETIME_HORIZON_YEARS))
    return hour_frac, day_of_week_frac, years_frac


def _write_record(
    record: RelationalRecord,
    *,
    position: int,
    vocab_size: int,
    numeric_values: torch.Tensor,
    numeric_mask: torch.Tensor,
    categorical_ids: torch.Tensor,
    categorical_mask: torch.Tensor,
    datetime_values: torch.Tensor,
    datetime_mask: torch.Tensor,
    entity_type_ids: torch.Tensor,
    relationship_ids: torch.Tensor,
    attention_mask: torch.Tensor,
    batch_index: int,
) -> None:
    schema = ENTITY_FIELD_SCHEMAS[record.entity]

    for slot, field_name in enumerate(schema.numeric_fields):
        value = record.fields.get(field_name)
        if value is not None:
            numeric_values[batch_index, position, slot] = float(value)
            numeric_mask[batch_index, position, slot] = True

    for slot, field_name in enumerate(schema.categorical_fields):
        value = record.fields.get(field_name)
        if value is not None:
            categorical_ids[batch_index, position, slot] = hash_categorical(
                str(value), vocab_size=vocab_size
            )
            categorical_mask[batch_index, position, slot] = True

    for slot, field_name in enumerate(schema.datetime_fields):
        value = record.fields.get(field_name)
        if isinstance(value, datetime):
            features = _encode_datetime(value)
            datetime_values[batch_index, position, slot, :] = torch.tensor(features)
            datetime_mask[batch_index, position, slot] = True

    entity_type_ids[batch_index, position] = _ENTITY_TYPE_INDEX[record.entity]
    relationship_ids[batch_index, position] = _RELATIONSHIP_INDEX[record.relationship]
    attention_mask[batch_index, position] = True


def encode_batch(
    examples: list[RelationalExample], *, max_records_per_relation: int, categorical_vocab_size: int
) -> EncodedBatch:
    batch_size = len(examples)
    seq_len = 1 + len(_RELATION_ORDER) * max_records_per_relation

    numeric_values = torch.zeros(batch_size, seq_len, NUMERIC_SLOTS)
    numeric_mask = torch.zeros(batch_size, seq_len, NUMERIC_SLOTS, dtype=torch.bool)
    categorical_ids = torch.zeros(batch_size, seq_len, CATEGORICAL_SLOTS, dtype=torch.long)
    categorical_mask = torch.zeros(batch_size, seq_len, CATEGORICAL_SLOTS, dtype=torch.bool)
    datetime_values = torch.zeros(batch_size, seq_len, DATETIME_SLOTS, DATETIME_FEATURES)
    datetime_mask = torch.zeros(batch_size, seq_len, DATETIME_SLOTS, dtype=torch.bool)
    entity_type_ids = torch.zeros(batch_size, seq_len, dtype=torch.long)
    relationship_ids = torch.zeros(batch_size, seq_len, dtype=torch.long)
    attention_mask = torch.zeros(batch_size, seq_len, dtype=torch.bool)

    has_labels = all(example.label is not None for example in examples)
    labels = torch.zeros(batch_size) if has_labels else None

    for batch_index, example in enumerate(examples):
        _write_record(
            example.target,
            position=0,
            vocab_size=categorical_vocab_size,
            numeric_values=numeric_values,
            numeric_mask=numeric_mask,
            categorical_ids=categorical_ids,
            categorical_mask=categorical_mask,
            datetime_values=datetime_values,
            datetime_mask=datetime_mask,
            entity_type_ids=entity_type_ids,
            relationship_ids=relationship_ids,
            attention_mask=attention_mask,
            batch_index=batch_index,
        )
        if labels is not None:
            assert example.label is not None  # nosec B101 - guarded by has_labels above
            labels[batch_index] = float(example.label)

        # Deterministic sampling (Phase 11 requirement 7): records within each relation are
        # already ordered by fetch_related_records (most-recent-first); this just places them
        # in that same order, truncated to the relation's K slots (unused slots stay padding).
        by_relation: dict[str, list[RelationalRecord]] = {name: [] for name in _RELATION_ORDER}
        for related_record in example.related:
            by_relation[related_record.relationship].append(related_record)

        for relation_index, relation_name in enumerate(_RELATION_ORDER):
            block_start = 1 + relation_index * max_records_per_relation
            records = by_relation[relation_name][:max_records_per_relation]
            for slot, related_record in enumerate(records):
                _write_record(
                    related_record,
                    position=block_start + slot,
                    vocab_size=categorical_vocab_size,
                    numeric_values=numeric_values,
                    numeric_mask=numeric_mask,
                    categorical_ids=categorical_ids,
                    categorical_mask=categorical_mask,
                    datetime_values=datetime_values,
                    datetime_mask=datetime_mask,
                    entity_type_ids=entity_type_ids,
                    relationship_ids=relationship_ids,
                    attention_mask=attention_mask,
                    batch_index=batch_index,
                )

    return EncodedBatch(
        numeric_values=numeric_values,
        numeric_mask=numeric_mask,
        categorical_ids=categorical_ids,
        categorical_mask=categorical_mask,
        datetime_values=datetime_values,
        datetime_mask=datetime_mask,
        entity_type_ids=entity_type_ids,
        relationship_ids=relationship_ids,
        attention_mask=attention_mask,
        labels=labels,
    )
