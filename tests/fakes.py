"""Fakes for tests that exercise connector/discovery logic without opening a real network
connection. Real connectivity is covered separately by tests/integration."""

from __future__ import annotations

from typing import Any

from hermes_rpt.connectors.errors import ConnectionFailedError
from hermes_rpt.connectors.interfaces import ConnectionTarget
from hermes_rpt.schemas.introspection import SchemaIntrospectionResult


class FakeEngine:
    def __init__(self, target: ConnectionTarget) -> None:
        self.target = target
        self.disposed = False
        self.should_fail_health_check = False


class FakeConnector:
    def __init__(self) -> None:
        self.built: list[FakeEngine] = []
        self.disposed: list[FakeEngine] = []

    def build_engine(self, target: ConnectionTarget) -> Any:
        engine = FakeEngine(target)
        self.built.append(engine)
        return engine

    async def check_connectivity(self, engine: Any) -> None:
        if engine.should_fail_health_check:
            raise ConnectionFailedError("simulated failure")

    async def dispose(self, engine: Any) -> None:
        engine.disposed = True
        self.disposed.append(engine)


class FakeIntrospector:
    """Returns a queue of canned `SchemaIntrospectionResult`s — one per call to `introspect`,
    in order — instead of running real SQL. Lets discovery-service tests exercise
    fingerprinting/drift/status-transition logic without a real Postgres instance."""

    def __init__(self, results: list[SchemaIntrospectionResult]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    async def introspect(
        self, engine: Any, *, schema_allowlist: list[str]
    ) -> SchemaIntrospectionResult:
        self.calls.append(schema_allowlist)
        if not self._results:
            raise AssertionError("FakeIntrospector has no more queued results")
        return self._results.pop(0)


class FailingIntrospector:
    async def introspect(
        self, engine: Any, *, schema_allowlist: list[str]
    ) -> SchemaIntrospectionResult:
        raise RuntimeError("simulated introspection failure")


class SQLiteConnector:
    """A `DatabaseConnector` backed by a real in-memory SQLite database instead of a live
    Postgres instance — used only by Phase 8 feature-extraction tests, where the compiler
    (`hermes_rpt.features.compiler`) executes real SQL and a placeholder `FakeEngine` (no
    `.connect()`) won't do. `build_engine` ignores `target` entirely and always returns the
    same shared engine, which the test populates with tables before extraction runs.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self.disposed: list[Any] = []

    def build_engine(self, target: ConnectionTarget) -> Any:
        return self._engine

    async def check_connectivity(self, engine: Any) -> None:
        return None

    async def dispose(self, engine: Any) -> None:
        self.disposed.append(engine)
