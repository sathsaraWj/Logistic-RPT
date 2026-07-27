"""Connection health-check service (Phase 4 requirement)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from hermes_rpt.connectors.errors import ConnectionFailedError
from hermes_rpt.connectors.interfaces import DatabaseConnector


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    healthy: bool
    checked_at: datetime
    error_summary: str | None = None


class ConnectionHealthChecker:
    def __init__(self, connector: DatabaseConnector) -> None:
        self._connector = connector

    async def check(self, engine: AsyncEngine) -> HealthCheckResult:
        checked_at = datetime.now(UTC)
        try:
            await self._connector.check_connectivity(engine)
        except ConnectionFailedError as exc:
            return HealthCheckResult(
                healthy=False, checked_at=checked_at, error_summary=exc.safe_summary
            )
        return HealthCheckResult(healthy=True, checked_at=checked_at)
