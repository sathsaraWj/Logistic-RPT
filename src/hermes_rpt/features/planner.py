"""Dry-run query plan (Phase 8): "a dry-run mode that displays safe query plans without
exposing secrets." Never resolves a secret, never opens a real connection — it only reports
what *would* be queried, from already-resolved mapping documents.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from hermes_rpt.features.compiler import resolve_column
from hermes_rpt.features.contract import FeatureContract, FeatureKind
from hermes_rpt.features.resolver import ResolvedMapping


class FeaturePlanEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    feature_name: str
    kind: FeatureKind
    available: bool
    reason: str | None = None
    source_table: str | None = None
    columns_referenced: tuple[str, ...] = ()


class QueryPlanReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_key: str
    target_entity: str
    target_source_table: str
    entries: tuple[FeaturePlanEntry, ...]


def build_query_plan(
    contract: FeatureContract,
    *,
    target: ResolvedMapping,
    related: dict[str, ResolvedMapping | None],
) -> QueryPlanReport:
    entries: list[FeaturePlanEntry] = []
    target_table = f"{target.document.source.schema_name}.{target.document.source.table}"

    for feature in contract.features:
        if feature.kind in (FeatureKind.DIRECT_FIELD, FeatureKind.DERIVED_FROM_TARGET):
            assert feature.target_field is not None  # nosec B101 - validated by FeatureSpec
            column = resolve_column(target.document, feature.target_field)
            entries.append(
                FeaturePlanEntry(
                    feature_name=feature.name,
                    kind=feature.kind,
                    available=column is not None,
                    reason=None if column is not None else "Target field is not mapped",
                    source_table=target_table,
                    columns_referenced=(column,) if column else (),
                )
            )
            continue

        assert feature.related_entity is not None  # nosec B101 - validated by FeatureSpec
        resolved_related = related.get(feature.related_entity)
        if resolved_related is None:
            entries.append(
                FeaturePlanEntry(
                    feature_name=feature.name,
                    kind=feature.kind,
                    available=False,
                    reason=f"No active production mapping for {feature.related_entity}",
                )
            )
            continue

        assert feature.related_fk_field_on_target is not None  # nosec B101
        join_column = resolve_column(target.document, feature.related_fk_field_on_target)
        related_join_column = resolve_column(
            resolved_related.document, feature.related_fk_field_on_target
        )
        columns = [c for c in (join_column, related_join_column) if c]
        available = join_column is not None and related_join_column is not None
        entries.append(
            FeaturePlanEntry(
                feature_name=feature.name,
                kind=feature.kind,
                available=available,
                reason=None if available else "Join field is not mapped on one side",
                source_table=(
                    f"{resolved_related.document.source.schema_name}."
                    f"{resolved_related.document.source.table}"
                ),
                columns_referenced=tuple(columns),
            )
        )

    return QueryPlanReport(
        task_key=contract.task_key,
        target_entity=contract.target_entity,
        target_source_table=target_table,
        entries=tuple(entries),
    )
