"""The declarative mapping document schema (Phase 7) — what a `MappingVersion.mapping_document`
actually contains, structurally validated by Pydantic before it is ever stored.

Supports (per Phase 7 requirements): direct column mapping, type conversion (implicit — the
target ontology field's `logical_type` is what values get coerced to), unit conversion, static
values, null handling, enumerated-value mapping, filters, joins through approved relationships,
derived fields (`hermes_rpt.mappings.expressions`), timestamp transformation, and source-priority
rules (`FieldMapping.sources` — an ordered list, first non-null wins).

No field here allows arbitrary Python or unrestricted SQL: `column` is a plain identifier
string (validated as such), filters/joins are structured comparisons, not SQL fragments.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hermes_rpt.mappings.expressions import Expression

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(value: str, *, what: str) -> str:
    if not _IDENTIFIER_PATTERN.match(value):
        raise ValueError(f"{what} must be a plain identifier, got {value!r}")
    return value


class SourceTable(BaseModel):
    # populate_by_name: accepts both the wire alias ("schema", the natural word for authors
    # writing YAML) and the Python field name ("schema_name", used when round-tripping via
    # model_dump(mode="json") without by_alias=True) — MappingService stores documents with
    # by_alias=True so the two stay consistent, but this also makes the model robust to
    # accidental round-trips that forget to pass it.
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    schema_name: str = Field(alias="schema")
    table: str

    @model_validator(mode="after")
    def _validate_identifiers(self) -> SourceTable:
        _validate_identifier(self.schema_name, what="source.schema")
        _validate_identifier(self.table, what="source.table")
        return self


class ValueSource(BaseModel):
    """One candidate source for a canonical field's value. A `FieldMapping.sources` list is
    tried in order (source-priority rules); the first one that resolves to a non-null value
    (after `null_values` substitution) wins."""

    model_config = ConfigDict(frozen=True)

    column: str | None = None
    static: Any | None = None
    derived: Expression | None = None
    source_unit: str | None = None
    source_timezone: str | None = None
    value_map: dict[str, str] | None = None
    null_values: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _exactly_one_source_kind(self) -> ValueSource:
        kinds_present = [
            self.column is not None,
            self.static is not None,
            self.derived is not None,
        ]
        if sum(kinds_present) != 1:
            raise ValueError("Exactly one of column, static, or derived must be set")
        if self.column is not None:
            _validate_identifier(self.column, what="column")
        if (self.source_unit or self.source_timezone or self.value_map) and self.column is None:
            raise ValueError("source_unit/source_timezone/value_map only apply to a column source")
        return self


class FieldMapping(BaseModel):
    model_config = ConfigDict(frozen=True)

    sources: tuple[ValueSource, ...] = Field(min_length=1)
    default: Any | None = None


class FilterCondition(BaseModel):
    """A single structured comparison — never a raw SQL fragment. `hermes_rpt.connectors.
    query_guard` still applies independently once this compiles to an actual query (Phase 8)."""

    model_config = ConfigDict(frozen=True)

    column: str
    equals: Any | None = None
    not_equals: Any | None = None
    is_null: bool | None = None

    @model_validator(mode="after")
    def _validate(self) -> FilterCondition:
        _validate_identifier(self.column, what="filter.column")
        specified = [self.equals is not None, self.not_equals is not None, self.is_null is not None]
        if sum(specified) != 1:
            raise ValueError("Exactly one of equals, not_equals, or is_null must be set")
        return self


class JoinDefinition(BaseModel):
    """A join is only valid if it fulfils a relationship the ontology entity actually declares
    — "joins through approved relationships" (Phase 7). `hermes_rpt.mappings.validation` checks
    `relationship` against the target `OntologyEntityDefinition`, and both tables against the
    connection's schema/table allowlist (the same allowlist `hermes_rpt.connectors.query_guard`
    enforces at query time)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    relationship: str
    schema_name: str = Field(alias="schema")
    table: str
    local_column: str
    foreign_column: str

    @model_validator(mode="after")
    def _validate_identifiers(self) -> JoinDefinition:
        _validate_identifier(self.schema_name, what="join.schema")
        _validate_identifier(self.table, what="join.table")
        _validate_identifier(self.local_column, what="join.local_column")
        _validate_identifier(self.foreign_column, what="join.foreign_column")
        return self


class MappingDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity: str
    source: SourceTable
    identity: dict[str, FieldMapping] = Field(min_length=1)
    fields: dict[str, FieldMapping] = Field(default_factory=dict)
    filters: tuple[FilterCondition, ...] = Field(default_factory=tuple)
    joins: tuple[JoinDefinition, ...] = Field(default_factory=tuple)
