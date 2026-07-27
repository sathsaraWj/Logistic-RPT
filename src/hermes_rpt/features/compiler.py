"""Safe query compiler (Phase 8).

Builds fully parameterized SQLAlchemy Core `select()` statements from a `MappingDocument`
(Phase 7) — never a string-formatted or model-generated SQL fragment. Every table/schema this
touches is checked against the connection's allowlist first
(`hermes_rpt.connectors.query_guard.assert_object_allowed`) — the same allowlist enforcement
Phase 4/5 use, applied again here since a mapping's allowlist could have been valid at approval
time and changed since. Every query carries a `LIMIT` (`hermes_rpt.features.cost_guard`) and,
for every related-entity query, a `< :prediction_time` predicate — the concrete mechanism behind
"a feature must only use information that would have existed at the prediction timestamp"
(Phase 8's leakage-prevention requirement).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from hermes_rpt.connectors.query_guard import assert_object_allowed
from hermes_rpt.features.contract import FeatureKind, FeatureSpec
from hermes_rpt.features.cost_guard import enforce_window_limit, related_row_limit
from hermes_rpt.mappings.document import MappingDocument


class ColumnNotDirectlyMappedError(Exception):
    """Raised when a join key, timestamp, or value field a feature needs is mapped via
    `static`/`derived` rather than a plain `column` — Phase 8 only builds joins/filters against
    real source columns, never through a derived expression."""


def resolve_column(document: MappingDocument, field_name: str) -> str | None:
    all_fields = {**document.identity, **document.fields}
    mapping = all_fields.get(field_name)
    if mapping is None:
        return None
    first_source = mapping.sources[0]
    if first_source.column is None:
        raise ColumnNotDirectlyMappedError(
            f"Field {field_name!r} is not mapped to a plain column (mapped via static/derived)"
        )
    return first_source.column


def _identity_column(document: MappingDocument) -> tuple[str, str]:
    """Returns (ontology_field_name, source_column_name) for the document's single identity
    field — Phase 8 features only ever key off one identity column per entity."""

    if len(document.identity) != 1:
        raise ColumnNotDirectlyMappedError(
            f"Expected exactly one identity field, found {list(document.identity)}"
        )
    (field_name,) = document.identity.keys()
    column = resolve_column(document, field_name)
    if column is None:
        raise ColumnNotDirectlyMappedError(f"Identity field {field_name!r} has no column mapping")
    return field_name, column


def _check_allowlisted(
    document: MappingDocument, *, schema_allowlist: list[str], table_allowlist: list[str]
) -> None:
    assert_object_allowed(
        schema=document.source.schema_name,
        table=document.source.table,
        schema_allowlist=schema_allowlist,
        table_allowlist=table_allowlist,
    )


async def fetch_target_row(
    engine: AsyncEngine,
    document: MappingDocument,
    *,
    business_reference: str,
    needed_fields: set[str],
    schema_allowlist: list[str],
    table_allowlist: list[str],
) -> dict[str, Any] | None:
    """Fetches exactly one row (by identity) with exactly the ontology fields the feature
    contract actually needs, each selected `.label()`-ed to its ontology field name."""

    _check_allowlisted(document, schema_allowlist=schema_allowlist, table_allowlist=table_allowlist)
    identity_field, identity_column = _identity_column(document)

    columns: dict[str, str] = {identity_field: identity_column}
    for field_name in needed_fields:
        if field_name == identity_field:
            continue
        column = resolve_column(document, field_name)
        if column is not None:
            columns[field_name] = column

    table = sa.table(document.source.table, *(sa.column(c) for c in columns.values()))
    table.schema = document.source.schema_name
    stmt = (
        sa.select(*(sa.column(c).label(name) for name, c in columns.items()))
        .select_from(table)
        .where(sa.column(identity_column) == sa.bindparam("business_reference"))
        .limit(1)
    )

    async with engine.connect() as conn:
        result = await conn.execute(stmt, {"business_reference": business_reference})
        row = result.mappings().first()
    return dict(row) if row is not None else None


async def compile_and_run_related_feature(
    engine: AsyncEngine,
    feature: FeatureSpec,
    document: MappingDocument,
    *,
    join_value: Any,
    prediction_time: datetime,
    schema_allowlist: list[str],
    table_allowlist: list[str],
) -> Any:
    """Runs the one query recipe `feature.kind` calls for against the related entity's mapped
    table. Returns the raw (not yet normalized) value — `hermes_rpt.features.normalizer`
    applies `data_type`/`missing_value_behavior` afterwards."""

    _check_allowlisted(document, schema_allowlist=schema_allowlist, table_allowlist=table_allowlist)
    assert feature.related_fk_field_on_target is not None  # nosec B101 - validated by FeatureSpec

    join_column = resolve_column(document, feature.related_fk_field_on_target)
    if join_column is None:
        return None  # related entity is mapped, but not on this exact join field — no data

    table = sa.table(document.source.table)
    table.schema = document.source.schema_name
    conditions = [sa.column(join_column) == sa.bindparam("join_value")]
    params: dict[str, Any] = {"join_value": join_value}

    timestamp_column = (
        resolve_column(document, feature.related_timestamp_field)
        if feature.related_timestamp_field
        else None
    )
    if timestamp_column is not None:
        conditions.append(sa.column(timestamp_column) < sa.bindparam("prediction_time"))
        params["prediction_time"] = prediction_time
        window_days = enforce_window_limit(feature.historical_window_days)
        if window_days is not None:
            window_start = prediction_time - timedelta(days=window_days)
            conditions.append(sa.column(timestamp_column) >= sa.bindparam("window_start"))
            params["window_start"] = window_start

    async with engine.connect() as conn:
        if feature.kind == FeatureKind.RELATED_COUNT:
            count_stmt = sa.select(sa.func.count()).select_from(table).where(*conditions)
            if feature.related_filter_field and feature.related_filter_equals is not None:
                filter_column = resolve_column(document, feature.related_filter_field)
                if filter_column is not None:
                    count_stmt = count_stmt.where(
                        sa.column(filter_column) == sa.bindparam("filter_value")
                    )
                    params["filter_value"] = feature.related_filter_equals
            result = await conn.execute(count_stmt, params)
            return result.scalar_one()

        if feature.kind == FeatureKind.RELATED_RATIO:
            assert feature.related_filter_field is not None  # nosec B101 - validated by FeatureSpec
            filter_column = resolve_column(document, feature.related_filter_field)
            if filter_column is None:
                return None
            matched_expr = sa.func.sum(
                sa.case((sa.column(filter_column) == sa.bindparam("filter_value"), 1), else_=0)
            )
            ratio_stmt = (
                sa.select(matched_expr, sa.func.count()).select_from(table).where(*conditions)
            )
            params["filter_value"] = feature.related_filter_equals
            result = await conn.execute(ratio_stmt, params)
            matched, total = result.one()
            return (matched / total) if total else None

        if feature.kind == FeatureKind.RELATED_RECENCY_DAYS:
            assert timestamp_column is not None  # nosec B101 - validated by FeatureSpec
            recency_stmt: sa.Select[Any] = (
                sa.select(sa.func.max(sa.column(timestamp_column)))
                .select_from(table)
                .where(*conditions)
            )
            result = await conn.execute(recency_stmt, params)
            raw_latest = result.scalar_one_or_none()
            if raw_latest is None:
                return None
            # Postgres/asyncpg always returns a real, timezone-aware datetime for a timestamp
            # column; this fallback exists only for portability with drivers (e.g. sqlite3,
            # used in some tests) that round-trip a TEXT-affinity column as a naive ISO string
            # instead — the platform's datetimes are always UTC (hermes_rpt.ontology.values.
            # CanonicalDatetime), so a naive value here is assumed to already be UTC.
            if isinstance(raw_latest, str):
                latest_ts = datetime.fromisoformat(raw_latest)
            else:
                latest_ts = raw_latest
            if latest_ts.tzinfo is None:
                latest_ts = latest_ts.replace(tzinfo=UTC)
            return (prediction_time - latest_ts).total_seconds() / 86400

        if feature.kind == FeatureKind.RELATED_LATEST_VALUE:
            assert feature.related_value_field is not None  # nosec B101 - validated by FeatureSpec
            value_column = resolve_column(document, feature.related_value_field)
            if value_column is None:
                return None
            latest_value_stmt: sa.Select[Any] = (
                sa.select(sa.column(value_column)).select_from(table).where(*conditions)
            )
            if timestamp_column is not None:
                latest_value_stmt = latest_value_stmt.order_by(sa.column(timestamp_column).desc())
            latest_value_stmt = latest_value_stmt.limit(related_row_limit())
            result = await conn.execute(latest_value_stmt, params)
            return result.scalars().first()

    raise ValueError(f"Unsupported feature kind: {feature.kind}")  # pragma: no cover
