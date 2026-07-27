"""Application-layer query guard: defense in depth on top of the database-level read-only
session setting (`hermes_rpt.connectors.postgres`).

Nothing in the platform accepts arbitrary SQL from a client (Phase 4 requirement — "do not
provide unrestricted raw SQL endpoints"); all queries against customer databases are composed
internally (schema discovery, feature extraction, in later phases) or are the fixed
introspection/health-check statements in this module's own callers. This guard exists so that
even *internally composed* SQL is checked twice: once for statement shape (read-only), once for
target-object shape (allowlisted schema/table).
"""

from __future__ import annotations

import re

from hermes_rpt.connectors.errors import DisallowedObjectError, WriteStatementRejectedError

# Matches the first SQL keyword, tolerating leading whitespace/comments/CTEs.
_WRITE_OR_DDL_KEYWORDS = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|MERGE|UPSERT|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|CALL|"
    r"COPY|VACUUM|EXECUTE|DO)\b",
    re.IGNORECASE,
)
_ALLOWED_LEADING_KEYWORDS = re.compile(r"^\s*(SELECT|WITH|EXPLAIN)\b", re.IGNORECASE)


def assert_read_only_statement(sql: str) -> None:
    """Rejects anything that isn't (very obviously) a read. This is a coarse guard, not a SQL
    parser — it exists to catch programming mistakes early with a clear error, not to be the
    only thing standing between the platform and a malicious query. The database session's
    `default_transaction_read_only=on` setting is the control that must hold even if this
    guard has a gap."""

    stripped = sql.strip()
    if _WRITE_OR_DDL_KEYWORDS.match(stripped):
        raise WriteStatementRejectedError(
            "Only read statements are permitted on customer database connections"
        )
    if not _ALLOWED_LEADING_KEYWORDS.match(stripped):
        raise WriteStatementRejectedError("Statement must start with SELECT, WITH, or EXPLAIN")


def assert_object_allowed(
    *, schema: str, table: str | None, schema_allowlist: list[str], table_allowlist: list[str]
) -> None:
    """`schema_allowlist`/`table_allowlist` come from `CustomerDatabaseConnection`. An empty
    allowlist means "nothing approved yet" — fail closed, not open — matching
    docs/IMPLEMENTATION_PLAN.md invariant #12."""

    if schema not in schema_allowlist:
        raise DisallowedObjectError(schema, table)
    if table is not None and table not in table_allowlist:
        raise DisallowedObjectError(schema, table)
