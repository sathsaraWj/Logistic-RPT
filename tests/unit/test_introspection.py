"""Tests for the introspector's fail-closed behaviour that don't require a real database."""

from __future__ import annotations

from hermes_rpt.schemas.introspection import PostgresSchemaIntrospector


async def test_empty_schema_allowlist_returns_empty_result_without_touching_the_engine() -> None:
    """An empty allowlist must short-circuit before ever using the engine — this is the same
    fail-closed policy as hermes_rpt.connectors.query_guard, tested here by passing `None` as
    the engine: if the guard didn't short-circuit, this would blow up immediately."""

    introspector = PostgresSchemaIntrospector()
    result = await introspector.introspect(None, schema_allowlist=[])  # type: ignore[arg-type]
    assert result.schemas == []
    assert result.tables == []
