"""Tests for the safe derived-field expression language (Phase 7)."""

from __future__ import annotations

import pytest

from hermes_rpt.mappings.expressions import (
    Coalesce,
    ColumnRef,
    Concat,
    ExpressionEvaluationError,
    IfNullThenElse,
    Literal_,
    LowerCase,
    Trim,
    UpperCase,
    evaluate,
    referenced_columns,
)


def test_column_ref_resolves_from_row() -> None:
    assert evaluate(ColumnRef(column="make"), row={"make": "Ford"}) == "Ford"


def test_unknown_column_ref_raises() -> None:
    with pytest.raises(ExpressionEvaluationError):
        evaluate(ColumnRef(column="missing"), row={})


def test_literal_returns_its_value() -> None:
    assert evaluate(Literal_(value=42), row={}) == 42


def test_concat_joins_parts_with_separator() -> None:
    expr = Concat(parts=(ColumnRef(column="a"), ColumnRef(column="b")), separator=" ")
    assert evaluate(expr, row={"a": "Ford", "b": "Transit"}) == "Ford Transit"


def test_concat_treats_none_as_empty_string() -> None:
    expr = Concat(parts=(ColumnRef(column="a"), Literal_(value="!")))
    assert evaluate(expr, row={"a": None}) == "!"


def test_coalesce_returns_first_non_null() -> None:
    expr = Coalesce(
        options=(ColumnRef(column="a"), ColumnRef(column="b"), Literal_(value="default"))
    )
    assert evaluate(expr, row={"a": None, "b": "value"}) == "value"


def test_coalesce_returns_none_if_all_null() -> None:
    expr = Coalesce(options=(ColumnRef(column="a"), ColumnRef(column="b")))
    assert evaluate(expr, row={"a": None, "b": None}) is None


def test_upper_lower_trim() -> None:
    assert evaluate(UpperCase(value=Literal_(value="abc")), row={}) == "ABC"
    assert evaluate(LowerCase(value=Literal_(value="ABC")), row={}) == "abc"
    assert evaluate(Trim(value=Literal_(value="  x  ")), row={}) == "x"


def test_upper_on_non_string_is_a_no_op() -> None:
    assert evaluate(UpperCase(value=Literal_(value=5)), row={}) == 5


def test_if_null_uses_fallback_only_when_null() -> None:
    present = IfNullThenElse(value=ColumnRef(column="a"), if_null=Literal_(value="fallback"))
    assert evaluate(present, row={"a": "value"}) == "value"
    assert evaluate(present, row={"a": None}) == "fallback"


def test_referenced_columns_collects_from_nested_expression() -> None:
    expr = Concat(
        parts=(
            ColumnRef(column="a"),
            IfNullThenElse(value=ColumnRef(column="b"), if_null=ColumnRef(column="c")),
        )
    )
    assert referenced_columns(expr) == {"a", "b", "c"}


def test_expression_tree_rejects_unrecognised_shape_at_validation_time() -> None:
    """Discriminated union: a `kind` that isn't in the fixed set fails Pydantic validation,
    not silent pass-through — "do not allow arbitrary Python" holds at the parsing boundary."""

    from pydantic import TypeAdapter, ValidationError

    from hermes_rpt.mappings.expressions import Expression

    adapter: TypeAdapter[object] = TypeAdapter(Expression)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "exec", "code": "import os"})
