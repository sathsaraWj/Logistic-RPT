"""Ontology registry: loads the bundled ontology definition and builds runtime Pydantic models
from it, so the canonical entity shapes (`Vehicle`, `Trip`, `FuelEvent`, ...) live in exactly
one place — the YAML — rather than being hand-duplicated into Python classes that could drift
from it. See docs/ONTOLOGY.md.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, create_model

from hermes_rpt.ontology.loader import load_ontology_from_yaml
from hermes_rpt.ontology.schema import (
    OntologyDefinition,
    OntologyEntityDefinition,
    OntologyFieldDefinition,
)
from hermes_rpt.ontology.values import CanonicalDatetime, LogicalType, Money

_ONTOLOGY_DIR = Path(__file__).resolve().parents[3] / "configs" / "ontology"
DEFAULT_ONTOLOGY_VERSION = "1"

_BASE_PYTHON_TYPES: dict[LogicalType, type] = {
    LogicalType.STRING: str,
    LogicalType.INTEGER: int,
    LogicalType.FLOAT: float,
    LogicalType.BOOLEAN: bool,
    LogicalType.DATE: date,
    LogicalType.DATETIME: CanonicalDatetime,
    LogicalType.MONEY: Money,
}


def _python_type_for_field(entity_name: str, field: OntologyFieldDefinition) -> Any:
    if field.logical_type == LogicalType.ENUM:
        assert field.enum_values  # nosec B101 - guaranteed by OntologyFieldDefinition validation
        return Enum(  # dynamic enum: values are canonical strings straight from the ontology
            f"{entity_name}_{field.name}_Enum",
            {value: value for value in field.enum_values},
            type=str,
        )
    return _BASE_PYTHON_TYPES[field.logical_type]


def build_pydantic_model(entity: OntologyEntityDefinition) -> type[BaseModel]:
    """Dynamically builds a Pydantic model class for one ontology entity. A required field has
    no default (Pydantic will reject a missing value); an optional field is `T | None = None`
    — "missing values are explicitly represented" (Phase 6) rather than a magic sentinel."""

    field_definitions: dict[str, Any] = {}
    for field in entity.fields:
        python_type = _python_type_for_field(entity.name, field)
        if field.required:
            field_definitions[field.name] = (python_type, ...)
        else:
            field_definitions[field.name] = (python_type | None, None)

    return create_model(entity.name, **field_definitions)


@lru_cache
def get_ontology(version: str = DEFAULT_ONTOLOGY_VERSION) -> OntologyDefinition:
    path = _ONTOLOGY_DIR / f"v{version}.yaml"
    return load_ontology_from_yaml(path)


@lru_cache
def get_entity_model(name: str, version: str = DEFAULT_ONTOLOGY_VERSION) -> type[BaseModel]:
    ontology = get_ontology(version)
    return build_pydantic_model(ontology.get_entity(name))


__all__ = ["build_pydantic_model", "get_entity_model", "get_ontology"]
