"""Meta-schema for ontology *definitions* — the shape of the YAML files under
`configs/ontology/`, not the canonical data itself. `hermes_rpt.ontology.loader` validates a
YAML file against this schema (structural validation) and then against additional semantic
rules (e.g. "a relationship's target entity must exist") that Pydantic alone can't express.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hermes_rpt.ontology.units import CanonicalUnit
from hermes_rpt.ontology.values import DataClassification, LogicalType, RelationshipCardinality


class OntologyFieldDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    logical_type: LogicalType
    unit: CanonicalUnit | None = None
    enum_values: tuple[str, ...] | None = None
    classification: DataClassification = DataClassification.INTERNAL
    required: bool = False
    is_business_identifier: bool = False

    @model_validator(mode="after")
    def _validate_type_specific_constraints(self) -> OntologyFieldDefinition:
        if self.logical_type == LogicalType.ENUM and not self.enum_values:
            raise ValueError(f"Field {self.name!r}: ENUM fields must set enum_values")
        if self.logical_type != LogicalType.ENUM and self.enum_values:
            raise ValueError(f"Field {self.name!r}: enum_values is only valid for ENUM fields")
        if self.unit is not None and self.logical_type != LogicalType.FLOAT:
            raise ValueError(f"Field {self.name!r}: unit is only valid for FLOAT fields")
        if self.is_business_identifier and self.logical_type != LogicalType.STRING:
            raise ValueError(
                f"Field {self.name!r}: business identifiers must be STRING "
                '("business IDs remain strings" — Phase 6)'
            )
        return self


class OntologyRelationshipDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    target_entity: str
    cardinality: RelationshipCardinality
    required: bool = False
    description: str = ""


class OntologyDomain:
    FLEET = "fleet"
    WORKFORCE = "workforce"
    OPERATIONS = "operations"
    MAINTENANCE = "maintenance"
    COST = "cost"


class OntologyEntityDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    domain: str
    description: str = ""
    fields: tuple[OntologyFieldDefinition, ...] = Field(default_factory=tuple)
    relationships: tuple[OntologyRelationshipDefinition, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_unique_field_names(self) -> OntologyEntityDefinition:
        names = [f.name for f in self.fields]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"Entity {self.name!r} has duplicate field names: {sorted(duplicates)}"
            )
        return self


class OntologyDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    entities: tuple[OntologyEntityDefinition, ...]

    @model_validator(mode="after")
    def _validate_unique_entity_names_and_relationship_targets(self) -> OntologyDefinition:
        names = {e.name for e in self.entities}
        if len(names) != len(self.entities):
            raise ValueError("Ontology has duplicate entity names")
        for entity in self.entities:
            for relationship in entity.relationships:
                if relationship.target_entity not in names:
                    raise ValueError(
                        f"Entity {entity.name!r} relationship {relationship.name!r} targets "
                        f"unknown entity {relationship.target_entity!r}"
                    )
        return self

    def get_entity(self, name: str) -> OntologyEntityDefinition:
        for entity in self.entities:
            if entity.name == name:
                return entity
        raise KeyError(f"Unknown ontology entity: {name!r}")
