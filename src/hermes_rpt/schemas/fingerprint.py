"""Deterministic fingerprints for schema/table/column/relationship shape (Phase 5).

Fingerprints are content hashes over *normalized, sorted* metadata — same shape in, same
fingerprint out, regardless of the order the database happened to return rows in. They hash
shape only (names, types, nullability, key/relationship structure), never row data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hermes_rpt.schemas.introspection import SchemaIntrospectionResult, TableMetadata


def _stable_hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_column(column_name: str, sql_type: str, is_nullable: bool) -> str:
    return _stable_hash({"name": column_name, "type": sql_type, "nullable": is_nullable})


def fingerprint_relationship(
    columns: list[str], referenced_table: str, referenced_columns: list[str]
) -> str:
    return _stable_hash(
        {
            "columns": sorted(columns),
            "referenced_table": referenced_table,
            "referenced_columns": sorted(referenced_columns),
        }
    )


def fingerprint_table(table: TableMetadata) -> str:
    column_fingerprints = sorted(
        fingerprint_column(c.name, c.sql_type, c.is_nullable) for c in table.columns
    )
    relationship_fingerprints = sorted(
        fingerprint_relationship(fk.columns, fk.referenced_table, fk.referenced_columns)
        for fk in table.foreign_keys
    )
    return _stable_hash(
        {
            "schema": table.schema_name,
            "name": table.name,
            "kind": table.kind,
            "columns": column_fingerprints,
            "primary_key": sorted(table.primary_key_columns),
            "relationships": relationship_fingerprints,
            "unique_constraints": sorted(
                _stable_hash(sorted(uq.columns)) for uq in table.unique_constraints
            ),
        }
    )


def compute_fingerprints(result: SchemaIntrospectionResult) -> tuple[str, dict[str, str]]:
    """Returns (schema_fingerprint, {qualified_table_name: table_fingerprint})."""

    table_fingerprints = {t.qualified_name: fingerprint_table(t) for t in result.tables}
    schema_fingerprint = _stable_hash(
        {"schemas": sorted(result.schemas), "tables": dict(sorted(table_fingerprints.items()))}
    )
    return schema_fingerprint, table_fingerprints
