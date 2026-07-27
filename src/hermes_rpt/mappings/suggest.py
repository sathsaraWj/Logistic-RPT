"""Deterministic mapping-suggestion engine (Phase 7).

Suggests a draft `MappingDocument` from a discovered `TableMetadata` (Phase 5) and an
`OntologyEntityDefinition` (Phase 6), using only: normalized column-name similarity, data-type
compatibility, key relationships (primary/foreign keys boost identifier-field matches), table/
column comments, and safe profile statistics (if present). **No external LLM call** (Phase 7
requirement) — see docs/adr/0007-deterministic-mapping-suggestions-before-llm.md. Every
suggestion carries a confidence score and an explanation, and — like every mapping, suggested or
hand-authored — requires human approval before it can become active
(`hermes_rpt.mappings.service`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from hermes_rpt.mappings.document import FieldMapping, MappingDocument, SourceTable, ValueSource
from hermes_rpt.ontology.schema import OntologyEntityDefinition, OntologyFieldDefinition
from hermes_rpt.ontology.values import LogicalType
from hermes_rpt.schemas.introspection import ColumnMetadata, TableMetadata
from hermes_rpt.schemas.profiling import TableProfileSummary

_TYPE_COMPATIBILITY: dict[LogicalType, set[str]] = {
    # Business-identifier source columns are very commonly integer surrogate keys (our own
    # Alpha/Beta fixtures use SERIAL primary keys) — "type conversion" support (Phase 7) means
    # a STRING ontology field is compatible with an integer-shaped source column, not just a
    # text-shaped one; it will be stringified on the way in.
    LogicalType.STRING: {
        "character varying",
        "character",
        "text",
        "uuid",
        "name",
        "citext",
        "integer",
        "bigint",
        "smallint",
    },
    LogicalType.INTEGER: {"integer", "bigint", "smallint", "numeric"},
    LogicalType.FLOAT: {"numeric", "real", "double precision", "integer", "bigint"},
    LogicalType.BOOLEAN: {"boolean"},
    LogicalType.DATE: {"date"},
    LogicalType.DATETIME: {
        "timestamp without time zone",
        "timestamp with time zone",
        "date",
    },
    LogicalType.ENUM: {"character varying", "character", "text"},
    LogicalType.MONEY: {"numeric", "money", "real", "double precision"},
}

_NORMALIZE_PATTERN = re.compile(r"[^a-z0-9]")


def _normalize(name: str) -> str:
    return _NORMALIZE_PATTERN.sub("", name.lower())


def _name_similarity(field_name: str, column_name: str) -> float:
    """Averages two signals rather than taking their max: character-level similarity
    (`SequenceMatcher`) and token-level (underscore-split) overlap. `max()` was tried first but
    lets either signal's noise dominate on short names — e.g. "vehicle_type_id" scoring an
    accidentally *higher* character-level ratio against "asset_id" than the semantically
    correct "vehicle_id" does, even though token overlap correctly prefers "vehicle_id". An
    average makes a single noisy signal much less able to flip the ranking on its own."""

    a, b = _normalize(field_name), _normalize(column_name)
    sequence_score = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(field_name.lower().split("_")), set(column_name.lower().split("_"))
    token_score = (
        len(tokens_a & tokens_b) / len(tokens_a | tokens_b) if (tokens_a or tokens_b) else 0.0
    )
    return (sequence_score + token_score) / 2


@dataclass(frozen=True, slots=True)
class ColumnScore:
    column: ColumnMetadata
    score: float
    explanation: str


def _score_column(
    field: OntologyFieldDefinition,
    column: ColumnMetadata,
    table: TableMetadata,
    profile: TableProfileSummary | None,
) -> ColumnScore:
    name_score = _name_similarity(field.name, column.name)
    type_compatible = column.sql_type in _TYPE_COMPATIBILITY.get(field.logical_type, set())
    reasons = [f"name similarity {name_score:.2f}"]

    score = name_score * 0.5
    score += 0.3 if type_compatible else 0.0
    reasons.append("type compatible" if type_compatible else "type NOT compatible")

    if field.is_business_identifier and column.name in table.primary_key_columns:
        score += 0.1
        reasons.append("column is the table's primary key")

    comment_text = " ".join(filter(None, [table.comment, column.comment])).lower()
    if comment_text and _normalize(field.name) in _normalize(comment_text):
        score += 0.1
        reasons.append("table/column comment mentions the field name")

    if profile is not None:
        column_profile = next((c for c in profile.columns if c.column_name == column.name), None)
        if column_profile is not None:
            if field.is_business_identifier and column_profile.distinct_count_estimate:
                uniqueness = column_profile.distinct_count_estimate / max(
                    profile.sampled_row_count, 1
                )
                if uniqueness > 0.95:
                    score += 0.05
                    reasons.append("profiled column looks highly unique (identifier-like)")
            if field.required and (column_profile.null_fraction or 0.0) > 0.5:
                score -= 0.1
                reasons.append("profiled column is mostly null, but field is required")

    return ColumnScore(column=column, score=min(score, 1.0), explanation="; ".join(reasons))


@dataclass(frozen=True, slots=True)
class SuggestionResult:
    document: MappingDocument
    confidence: float
    explanation: str


_MIN_CONFIDENCE = 0.35


def suggest_mapping(
    *,
    ontology_entity: OntologyEntityDefinition,
    table: TableMetadata,
    profile: TableProfileSummary | None = None,
) -> SuggestionResult:
    """Global greedy assignment: every (field, column) pair is scored once, then pairs are
    claimed strongest-first. This — rather than resolving one field at a time — is what stops
    a weak match for one field (e.g. a second identifier-shaped field with no real counterpart
    in this table) from grabbing a column a *different* field would have matched strongly."""

    used_columns: set[str] = set()
    used_fields: set[str] = set()
    field_explanations: list[str] = []
    identity: dict[str, FieldMapping] = {}
    fields: dict[str, FieldMapping] = {}
    confidences: list[float] = []

    all_scores = [
        (field, _score_column(field, column, table, profile))
        for field in ontology_entity.fields
        for column in table.columns
    ]
    all_scores.sort(key=lambda pair: pair[1].score, reverse=True)

    for field, candidate in all_scores:
        if candidate.score < _MIN_CONFIDENCE:
            break  # sorted descending — nothing after this is confident enough either
        if field.name in used_fields or candidate.column.name in used_columns:
            continue

        used_columns.add(candidate.column.name)
        used_fields.add(field.name)
        mapping = FieldMapping(sources=(ValueSource(column=candidate.column.name),))
        target = identity if field.is_business_identifier else fields
        target[field.name] = mapping
        confidences.append(candidate.score)
        field_explanations.append(
            f"{field.name} <- {candidate.column.name} ({candidate.explanation})"
        )

    if not identity:
        # Nothing usable was found at all for any identifier field — still produce a
        # structurally valid (if empty-identity) document is not allowed by MappingDocument's
        # own validation (identity requires at least one entry), so raise clearly instead of
        # producing something that will fail Pydantic validation with a confusing message.
        raise ValueError(
            f"Could not confidently suggest any identifier field mapping for entity "
            f"{ontology_entity.name!r} against table {table.qualified_name!r}"
        )

    document = MappingDocument(
        entity=ontology_entity.name,
        source=SourceTable(schema=table.schema_name, table=table.name),
        identity=identity,
        fields=fields,
    )
    overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    explanation = (
        f"Suggested {len(identity) + len(fields)} field(s) for {ontology_entity.name!r} from "
        f"{table.qualified_name!r}: " + "; ".join(field_explanations)
    )
    return SuggestionResult(
        document=document, confidence=overall_confidence, explanation=explanation
    )
