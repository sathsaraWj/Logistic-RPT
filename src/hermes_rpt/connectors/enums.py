from __future__ import annotations

from enum import StrEnum


class DatabaseEngine(StrEnum):
    """PostgreSQL is functional from Phase 4; the others are reserved interface targets
    (docs/IMPLEMENTATION_PLAN.md Phase 4) and are not required to be connectable yet."""

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MSSQL = "mssql"
    ORACLE = "oracle"


class ConnectionStatus(StrEnum):
    PENDING_VALIDATION = "pending_validation"
    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"
