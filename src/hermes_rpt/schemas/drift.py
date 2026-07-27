"""Schema-drift comparison (Phase 5).

Compares two `SchemaIntrospectionResult`s (typically consecutive snapshots for the same
connection) and reports what changed. Renamed-looking tables are *flagged*, never
auto-resolved — "do not automatically approve inferred renames" (Phase 5 requirement); a
rename suggestion is reported as a removed table + an added table with a note, and it is
Phase 7's mapping-approval workflow, not this module, that ever treats it as confirmed.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from hermes_rpt.schemas.fingerprint import fingerprint_column
from hermes_rpt.schemas.introspection import SchemaIntrospectionResult, TableMetadata

# Two tables are "likely the same table renamed" if they share at least this fraction of
# column fingerprints — a coarse heuristic, not a claim of certainty (see module docstring).
_RENAME_SIMILARITY_THRESHOLD = 0.7


class DriftEventType(StrEnum):
    TABLE_ADDED = "table_added"
    TABLE_REMOVED = "table_removed"
    TABLE_RENAMED_LIKELY = "table_renamed_likely"
    COLUMN_ADDED = "column_added"
    COLUMN_REMOVED = "column_removed"
    COLUMN_TYPE_CHANGED = "column_type_changed"
    COLUMN_NULLABILITY_CHANGED = "column_nullability_changed"
    RELATIONSHIP_CHANGED = "relationship_changed"


class DriftEvent(BaseModel):
    event_type: DriftEventType
    table: str
    detail: str


def _column_fingerprint_set(table: TableMetadata) -> set[str]:
    return {fingerprint_column(c.name, c.sql_type, c.is_nullable) for c in table.columns}


_RelationshipSignatureEntry = tuple[str, tuple[str, ...], str]


def _relationship_signature(table: TableMetadata) -> frozenset[_RelationshipSignatureEntry]:
    return frozenset(
        (fk.constraint_name, tuple(sorted(fk.columns)), fk.referenced_table)
        for fk in table.foreign_keys
    )


def _likely_rename_target(
    removed: TableMetadata, added_tables: list[TableMetadata]
) -> TableMetadata | None:
    removed_columns = _column_fingerprint_set(removed)
    if not removed_columns:
        return None
    best: tuple[float, TableMetadata] | None = None
    for candidate in added_tables:
        candidate_columns = _column_fingerprint_set(candidate)
        if not candidate_columns:
            continue
        overlap = len(removed_columns & candidate_columns)
        similarity = overlap / max(len(removed_columns), len(candidate_columns))
        if similarity >= _RENAME_SIMILARITY_THRESHOLD and (best is None or similarity > best[0]):
            best = (similarity, candidate)
    return best[1] if best else None


def compare_snapshots(
    old: SchemaIntrospectionResult, new: SchemaIntrospectionResult
) -> list[DriftEvent]:
    events: list[DriftEvent] = []

    old_by_name = {t.qualified_name: t for t in old.tables}
    new_by_name = {t.qualified_name: t for t in new.tables}

    removed_names = set(old_by_name) - set(new_by_name)
    added_names = set(new_by_name) - set(old_by_name)
    common_names = set(old_by_name) & set(new_by_name)

    added_tables = [new_by_name[name] for name in added_names]
    renamed_pairs: dict[str, str] = {}
    for removed_name in sorted(removed_names):
        match = _likely_rename_target(old_by_name[removed_name], added_tables)
        if match is not None:
            renamed_pairs[removed_name] = match.qualified_name
            events.append(
                DriftEvent(
                    event_type=DriftEventType.TABLE_RENAMED_LIKELY,
                    table=removed_name,
                    detail=(
                        f"'{removed_name}' looks like it may have been renamed to "
                        f"'{match.qualified_name}' (similar columns) — not auto-approved"
                    ),
                )
            )

    for removed_name in sorted(removed_names - set(renamed_pairs)):
        events.append(
            DriftEvent(
                event_type=DriftEventType.TABLE_REMOVED, table=removed_name, detail="Table removed"
            )
        )
    renamed_targets = set(renamed_pairs.values())
    for added_name in sorted(added_names - renamed_targets):
        events.append(
            DriftEvent(
                event_type=DriftEventType.TABLE_ADDED, table=added_name, detail="Table added"
            )
        )

    for name in sorted(common_names):
        old_table, new_table = old_by_name[name], new_by_name[name]
        old_columns = {c.name: c for c in old_table.columns}
        new_columns = {c.name: c for c in new_table.columns}

        for col_name in sorted(set(new_columns) - set(old_columns)):
            events.append(
                DriftEvent(
                    event_type=DriftEventType.COLUMN_ADDED,
                    table=name,
                    detail=f"Column '{col_name}' added",
                )
            )
        for col_name in sorted(set(old_columns) - set(new_columns)):
            events.append(
                DriftEvent(
                    event_type=DriftEventType.COLUMN_REMOVED,
                    table=name,
                    detail=f"Column '{col_name}' removed",
                )
            )
        for col_name in sorted(set(old_columns) & set(new_columns)):
            old_col, new_col = old_columns[col_name], new_columns[col_name]
            if old_col.sql_type != new_col.sql_type:
                events.append(
                    DriftEvent(
                        event_type=DriftEventType.COLUMN_TYPE_CHANGED,
                        table=name,
                        detail=(
                            f"Column '{col_name}' type changed from "
                            f"'{old_col.sql_type}' to '{new_col.sql_type}'"
                        ),
                    )
                )
            if old_col.is_nullable != new_col.is_nullable:
                events.append(
                    DriftEvent(
                        event_type=DriftEventType.COLUMN_NULLABILITY_CHANGED,
                        table=name,
                        detail=(
                            f"Column '{col_name}' nullability changed from "
                            f"{old_col.is_nullable} to {new_col.is_nullable}"
                        ),
                    )
                )

        if _relationship_signature(old_table) != _relationship_signature(new_table):
            events.append(
                DriftEvent(
                    event_type=DriftEventType.RELATIONSHIP_CHANGED,
                    table=name,
                    detail="Foreign key relationships changed",
                )
            )

    return events
