"""Query guard tests: statement shape and target-object allowlisting."""

from __future__ import annotations

import pytest

from hermes_rpt.connectors.errors import DisallowedObjectError, WriteStatementRejectedError
from hermes_rpt.connectors.query_guard import assert_object_allowed, assert_read_only_statement


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1", "  select * from foo", "WITH x AS (SELECT 1) SELECT * FROM x", "EXPLAIN SELECT 1"],
)
def test_read_statements_are_allowed(sql: str) -> None:
    assert_read_only_statement(sql)  # must not raise


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO foo VALUES (1)",
        "UPDATE foo SET x = 1",
        "DELETE FROM foo",
        "DROP TABLE foo",
        "TRUNCATE foo",
        "ALTER TABLE foo ADD COLUMN y INT",
        "GRANT ALL ON foo TO bar",
        "CALL some_procedure()",
        "; SELECT 1; DROP TABLE foo; --",
    ],
)
def test_write_and_ddl_statements_are_rejected(sql: str) -> None:
    with pytest.raises(WriteStatementRejectedError):
        assert_read_only_statement(sql)


def test_unrecognised_leading_keyword_is_rejected() -> None:
    with pytest.raises(WriteStatementRejectedError):
        assert_read_only_statement("VACUUM ANALYZE foo")


def test_object_on_allowlist_is_allowed() -> None:
    assert_object_allowed(
        schema="public",
        table="fleet_vehicle",
        schema_allowlist=["public"],
        table_allowlist=["fleet_vehicle"],
    )


def test_schema_not_on_allowlist_is_rejected() -> None:
    with pytest.raises(DisallowedObjectError):
        assert_object_allowed(
            schema="pg_catalog",
            table="pg_user",
            schema_allowlist=["public"],
            table_allowlist=["fleet_vehicle"],
        )


def test_table_not_on_allowlist_is_rejected() -> None:
    with pytest.raises(DisallowedObjectError):
        assert_object_allowed(
            schema="public",
            table="secret_table",
            schema_allowlist=["public"],
            table_allowlist=["fleet_vehicle"],
        )


def test_empty_allowlists_fail_closed() -> None:
    with pytest.raises(DisallowedObjectError):
        assert_object_allowed(
            schema="public", table="fleet_vehicle", schema_allowlist=[], table_allowlist=[]
        )
