"""Self-supervised relational pretraining objectives for Hermes-RPT (Phase 12).

Three real, separately-testable objectives, all built on the same `EncodedBatch` shape Phase 11
already established (never a second, parallel data path):

* **Masked cell reconstruction** — covers "masked categorical-value prediction" and "numeric-
  value reconstruction" via one mechanism across all three field kinds. A masked cell is made to
  look exactly like a *missing* one to the encoder (same masking machinery Phase 11 already has
  for genuinely-missing fields), and a separate target records what the value actually was.
* **Relationship-link prediction** (also covers "foreign-key target prediction" — a corrupted
  link *is* a wrong foreign-key target) — a subset of related-record positions have their
  content swapped for a different, cross-entity-type related record's content while keeping the
  position's claimed relationship/entity-type embeddings unchanged; the model must detect the
  mismatch.
* **Temporal-order prediction** — for pairs of related records of the same entity type (both
  with a usable timestamp), predict which one happened first.

Two objectives from the phase's full example list are **not implemented** here, and that is a
documented scope decision, not a silent gap: table/column semantic alignment (this platform maps
every tenant's heterogeneous schema onto one shared canonical ontology *before* anything reaches
this model — there is no second, differently-shaped ontology to align against within this
phase's scope) and record-context matching (its natural formulation overlaps heavily with
relationship-link prediction as implemented above; adding a second near-duplicate mechanism
wasn't judged worth the complexity this phase). See docs/HERMES_RPT_PRETRAINING.md.

"Avoid objectives that expose direct identifiers unnecessarily" / "avoid masking protected
fields into memorisation targets": `protected_field_names` reads `OntologyFieldDefinition.
is_business_identifier` (Phase 6) directly — a field never becomes a reconstruction target
because it is protected *by the same metadata Phase 6 already uses to mark it sensitive*, not by
a second, hand-maintained list that could drift out of sync.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import torch

from hermes_rpt.models.transformer.encoding import EncodedBatch
from hermes_rpt.models.transformer.schema import (
    ENTITY_FIELD_SCHEMAS,
    ENTITY_TYPE_VOCAB,
    RELATIONSHIP_VOCAB,
)
from hermes_rpt.ontology.registry import get_ontology

_TARGET_RELATIONSHIP_INDEX = RELATIONSHIP_VOCAB.index("__target__")
_PAD_ENTITY_INDEX = ENTITY_TYPE_VOCAB.index("__pad__")


@dataclass(frozen=True, slots=True)
class MaskingConfig:
    mask_probability: float = 0.2
    link_corruption_probability: float = 0.3
    temporal_pair_probability: float = 0.5
    random_seed: int = 0


def protected_field_names(entity: str) -> frozenset[str]:
    ontology_entity = get_ontology().get_entity(entity)
    return frozenset(f.name for f in ontology_entity.fields if f.is_business_identifier)


def _protected_sets() -> dict[str, frozenset[str]]:
    return {entity: protected_field_names(entity) for entity in ENTITY_FIELD_SCHEMAS}


@dataclass(frozen=True, slots=True)
class ReconstructionTargets:
    numeric_target_mask: torch.Tensor
    numeric_targets: torch.Tensor
    categorical_target_mask: torch.Tensor
    categorical_targets: torch.Tensor
    datetime_target_mask: torch.Tensor
    datetime_targets: torch.Tensor
    link_positions: torch.Tensor  # (B, S) bool — eligible non-target, non-padding positions
    link_labels: torch.Tensor  # (B, S) float — 1.0 genuine, 0.0 corrupted
    # (batch_index, position_a, position_b, label) — label=1 iff record a is temporally before b
    temporal_pairs: list[tuple[int, int, int, int]] = field(default_factory=list)


def build_pretraining_batch(
    batch: EncodedBatch, *, config: MaskingConfig
) -> tuple[EncodedBatch, ReconstructionTargets]:
    """Never crosses a tenant or example boundary — every swap/pair this function builds is
    drawn from *within* the same already-tenant-scoped example, so "preserve tenant boundaries"
    holds by construction, the same way it does everywhere else `EncodedBatch` is used."""

    rng = random.Random(config.random_seed)  # noqa: S311  # nosec B311 - masking, not crypto
    protected = _protected_sets()

    numeric_values = batch.numeric_values.clone()
    numeric_mask = batch.numeric_mask.clone()
    categorical_ids = batch.categorical_ids.clone()
    categorical_mask = batch.categorical_mask.clone()
    datetime_values = batch.datetime_values.clone()
    datetime_mask = batch.datetime_mask.clone()

    numeric_target_mask = torch.zeros_like(batch.numeric_mask)
    categorical_target_mask = torch.zeros_like(batch.categorical_mask)
    datetime_target_mask = torch.zeros_like(batch.datetime_mask)
    link_positions = torch.zeros_like(batch.attention_mask)
    link_labels = torch.zeros(batch.attention_mask.shape)
    temporal_pairs: list[tuple[int, int, int, int]] = []

    batch_size, seq_len = batch.attention_mask.shape
    for b in range(batch_size):
        real_positions = [s for s in range(seq_len) if bool(batch.attention_mask[b, s])]
        related_positions = [
            s
            for s in real_positions
            if int(batch.relationship_ids[b, s]) != _TARGET_RELATIONSHIP_INDEX
        ]

        for s in real_positions:
            entity_index = int(batch.entity_type_ids[b, s])
            if entity_index == _PAD_ENTITY_INDEX:
                continue
            entity = ENTITY_TYPE_VOCAB[entity_index]
            schema = ENTITY_FIELD_SCHEMAS[entity]
            entity_protected = protected[entity]

            for slot, field_name in enumerate(schema.numeric_fields):
                if field_name in entity_protected or not batch.numeric_mask[b, s, slot]:
                    continue
                if rng.random() < config.mask_probability:
                    numeric_target_mask[b, s, slot] = True
                    numeric_mask[b, s, slot] = False
                    numeric_values[b, s, slot] = 0.0

            for slot, field_name in enumerate(schema.categorical_fields):
                if field_name in entity_protected or not batch.categorical_mask[b, s, slot]:
                    continue
                if rng.random() < config.mask_probability:
                    categorical_target_mask[b, s, slot] = True
                    categorical_mask[b, s, slot] = False
                    categorical_ids[b, s, slot] = 0

            for slot, field_name in enumerate(schema.datetime_fields):
                if field_name in entity_protected or not batch.datetime_mask[b, s, slot]:
                    continue
                if rng.random() < config.mask_probability:
                    datetime_target_mask[b, s, slot] = True
                    datetime_mask[b, s, slot] = False
                    datetime_values[b, s, slot, :] = 0.0

        for s in related_positions:
            link_positions[b, s] = True
            link_labels[b, s] = 1.0
            if rng.random() >= config.link_corruption_probability:
                continue
            cross_entity_candidates = [
                p
                for p in related_positions
                if int(batch.entity_type_ids[b, p]) != int(batch.entity_type_ids[b, s])
            ]
            if not cross_entity_candidates:
                continue
            other = rng.choice(cross_entity_candidates)
            numeric_values[b, s] = batch.numeric_values[b, other]
            numeric_mask[b, s] = batch.numeric_mask[b, other]
            categorical_ids[b, s] = batch.categorical_ids[b, other]
            categorical_mask[b, s] = batch.categorical_mask[b, other]
            datetime_values[b, s] = batch.datetime_values[b, other]
            datetime_mask[b, s] = batch.datetime_mask[b, other]
            link_labels[b, s] = 0.0

        by_entity: dict[int, list[int]] = {}
        for s in related_positions:
            by_entity.setdefault(int(batch.entity_type_ids[b, s]), []).append(s)
        for positions in by_entity.values():
            candidates = [s for s in positions if datetime_mask[b, s, 0]]
            if len(candidates) < 2 or rng.random() > config.temporal_pair_probability:
                continue
            pos_a, pos_b = rng.sample(candidates, 2)
            years_a = batch.datetime_values[b, pos_a, 0, 2]
            years_b = batch.datetime_values[b, pos_b, 0, 2]
            label = 1 if years_a < years_b else 0
            temporal_pairs.append((b, pos_a, pos_b, label))

    masked_batch = EncodedBatch(
        numeric_values=numeric_values,
        numeric_mask=numeric_mask,
        categorical_ids=categorical_ids,
        categorical_mask=categorical_mask,
        datetime_values=datetime_values,
        datetime_mask=datetime_mask,
        entity_type_ids=batch.entity_type_ids,
        relationship_ids=batch.relationship_ids,
        attention_mask=batch.attention_mask,
        labels=batch.labels,
    )
    targets = ReconstructionTargets(
        numeric_target_mask=numeric_target_mask,
        numeric_targets=batch.numeric_values,
        categorical_target_mask=categorical_target_mask,
        categorical_targets=batch.categorical_ids,
        datetime_target_mask=datetime_target_mask,
        datetime_targets=batch.datetime_values,
        link_positions=link_positions,
        link_labels=link_labels,
        temporal_pairs=temporal_pairs,
    )
    return masked_batch, targets
