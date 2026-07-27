"""Sanitized connector errors — "never include connection strings in logs or exceptions"
(Phase 4 requirement).

Driver exceptions (asyncpg, SQLAlchemy) do not normally embed the password, but they can embed
host/database/username, and a careless `str(exc)` or `repr(engine.url)` elsewhere absolutely
can. `ConnectionFailedError` is what connector code raises instead of letting a raw driver
exception propagate, and it carries only a caller-supplied safe summary.
"""

from __future__ import annotations


class ConnectionFailedError(Exception):
    def __init__(self, safe_summary: str) -> None:
        self.safe_summary = safe_summary
        super().__init__(safe_summary)


class WriteStatementRejectedError(Exception):
    """Raised by the query guard when a caller attempts anything other than a read on a
    customer database connection — "prevent write statements" (Phase 4 requirement)."""


class DisallowedObjectError(Exception):
    """Raised when a query targets a schema/table not on the connection's allowlist."""

    def __init__(self, schema: str, table: str | None = None) -> None:
        target = f"{schema}.{table}" if table else schema
        super().__init__(f"{target!r} is not on the approved allowlist for this connection")
