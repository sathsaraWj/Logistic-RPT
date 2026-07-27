"""Loads and validates an ontology definition from YAML (Phase 6 requirement: "ontology
definitions in machine-readable YAML or JSON").
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from hermes_rpt.ontology.schema import OntologyDefinition


class OntologyValidationError(Exception):
    pass


def load_ontology_from_yaml(path: Path) -> OntologyDefinition:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OntologyValidationError(f"{path}: invalid YAML: {exc}") from exc

    try:
        return OntologyDefinition.model_validate(raw)
    except ValidationError as exc:
        raise OntologyValidationError(f"{path}: ontology definition is invalid: {exc}") from exc
