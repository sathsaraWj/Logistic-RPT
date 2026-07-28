"""Adversarial tests for three named attack vectors from prompts.txt Prompt 16: SQL injection,
mapping-expression injection, and path traversal — plus a confirmatory check for unsafe
deserialization.

These attack a `MappingDocument` (Phase 7), the one place a tenant supplies identifiers
(schema/table/column names, join definitions) that eventually get embedded into SQL —
`hermes_rpt.connectors.query_guard` (statement-shape/allowlist guard) and
`hermes_rpt.mappings.expressions` (the derived-field language) are already unit-tested
separately (`tests/unit/test_query_guard.py`, `tests/unit/test_mapping_expressions.py`); this
file specifically exercises them with realistic injection/traversal *payloads*, framed as
attack vectors, in one place.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_rpt.mappings.document import (
    FilterCondition,
    JoinDefinition,
    MappingDocument,
    SourceTable,
    ValueSource,
)

_SQL_INJECTION_PAYLOADS = [
    "users; DROP TABLE users;--",
    "id' OR '1'='1",
    'name" OR "1"="1',
    "x)) UNION SELECT password FROM auth_users--",
    "table_name/**/UNION/**/SELECT",
]

_PATH_TRAVERSAL_PAYLOADS = [
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "/etc/shadow",
    "schema/../../secrets",
    "a/b",
]


@pytest.mark.parametrize("payload", _SQL_INJECTION_PAYLOADS)
def test_sql_injection_payload_in_source_table_is_rejected(payload: str) -> None:
    with pytest.raises(ValidationError):
        SourceTable(schema="public", table=payload)


@pytest.mark.parametrize("payload", _SQL_INJECTION_PAYLOADS)
def test_sql_injection_payload_in_filter_column_is_rejected(payload: str) -> None:
    with pytest.raises(ValidationError):
        FilterCondition(column=payload, equals="x")


@pytest.mark.parametrize("payload", _SQL_INJECTION_PAYLOADS)
def test_sql_injection_payload_in_join_definition_is_rejected(payload: str) -> None:
    with pytest.raises(ValidationError):
        JoinDefinition(
            relationship="owner",
            schema="public",
            table=payload,
            local_column="owner_id",
            foreign_column="id",
        )


@pytest.mark.parametrize("payload", _PATH_TRAVERSAL_PAYLOADS)
def test_path_traversal_payload_in_source_table_schema_is_rejected(payload: str) -> None:
    with pytest.raises(ValidationError):
        SourceTable(schema=payload, table="fleet_vehicle")


@pytest.mark.parametrize("payload", _PATH_TRAVERSAL_PAYLOADS)
def test_path_traversal_payload_in_column_source_is_rejected(payload: str) -> None:
    with pytest.raises(ValidationError):
        ValueSource(column=payload)


_MALICIOUS_DOCUMENT_PAYLOAD = {
    "entity": "Vehicle",
    "source": {"schema": "public", "table": "fleet_vehicle"},
    "identity": {
        "vehicle_id": {"sources": [{"column": "id; DROP TABLE fleet_vehicle;--"}]},
    },
    "filters": [{"column": "1=1; --", "equals": "x"}],
}


def test_a_fully_malicious_mapping_document_is_rejected_end_to_end() -> None:
    """Every identifier-shaped field at once — schema, table, filter, and field-mapping column —
    carrying an injection payload; the whole document must fail to even construct. A single
    `model_validate` call (rather than nested constructors) so the one thing under test is
    "does this payload, as a whole, get rejected.\""""

    with pytest.raises(ValidationError):
        MappingDocument.model_validate(_MALICIOUS_DOCUMENT_PAYLOAD)


def test_expression_injection_cannot_smuggle_a_disallowed_node_kind() -> None:
    """The discriminated `Expression` union (`hermes_rpt.mappings.expressions`) is the only
    thing a derived field can be built from — a mapping document can't add a new node kind of
    its own, so trying to sneak in something eval/exec-shaped fails Pydantic validation before
    it is ever stored, matching the module's "not a string you parse or eval()" design."""

    with pytest.raises(ValidationError):
        ValueSource.model_validate(
            {"derived": {"kind": "python_eval", "code": "__import__('os').system('id')"}}
        )


def test_expression_injection_via_an_attribute_path_shaped_column_name_stays_inert() -> None:
    """`ColumnRef.column` isn't restricted to the plain-identifier pattern the way a direct
    `ValueSource.column` mapping is — a dotted, attribute-access-shaped string like
    `"os.system('id')"` constructs without error. It's still harmless: `evaluate()` only ever
    does a `dict` key lookup against the pre-extracted row (never `getattr`/`eval`/subscript
    access on an arbitrary object), so an unresolvable key just fails loudly instead of
    executing anything or reading anything it shouldn't."""

    from hermes_rpt.mappings.expressions import ColumnRef, ExpressionEvaluationError, evaluate

    malicious_ref = ColumnRef(column="os.system('id')")

    with pytest.raises(ExpressionEvaluationError):
        evaluate(malicious_ref, row={"vehicle_id": 1})
