"""A safe, structured expression language for derived mapping fields (Phase 7).

Deliberately **not** a string you parse or `eval()` — an expression is a small, closed set of
Pydantic node types (this module has the complete list; there is no way to extend it from a
mapping document itself), evaluated by `evaluate()` below, which never calls `eval`/`exec`/
`compile`, never imports anything named by data, and never does attribute/subscript access on
arbitrary Python objects. This is the concrete mechanism behind "do not allow arbitrary Python
or unrestricted SQL in mapping definitions" (Phase 7 requirement) for the derived-field case.

Every node's `kind` is a `Literal[...]` discriminator so a mapping document with an unrecognised
or malformed expression fails Pydantic validation before it is ever stored, let alone evaluated.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ScalarValue = str | float | int | bool | None


class ColumnRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["column"] = "column"
    column: str


class Literal_(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["literal"] = "literal"
    value: ScalarValue


class Concat(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["concat"] = "concat"
    parts: tuple[Expression, ...]
    separator: str = ""


class Coalesce(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["coalesce"] = "coalesce"
    options: tuple[Expression, ...]


class UpperCase(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["upper"] = "upper"
    value: Expression


class LowerCase(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["lower"] = "lower"
    value: Expression


class Trim(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["trim"] = "trim"
    value: Expression


class IfNullThenElse(BaseModel):
    """The only conditional node: `if value is null (or missing), use `if_null`, otherwise
    `otherwise`. Deliberately narrower than a general if/else with arbitrary comparisons — the
    only condition expressible is nullness, which covers the great majority of real-world
    "pick a fallback source" mapping needs without a comparison-operator surface to secure."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["if_null"] = "if_null"
    value: Expression
    if_null: Expression


Expression = Annotated[
    ColumnRef | Literal_ | Concat | Coalesce | UpperCase | LowerCase | Trim | IfNullThenElse,
    Field(discriminator="kind"),
]

Concat.model_rebuild()
Coalesce.model_rebuild()
UpperCase.model_rebuild()
LowerCase.model_rebuild()
Trim.model_rebuild()
IfNullThenElse.model_rebuild()


class ExpressionEvaluationError(Exception):
    pass


class ExpressionTooDeepError(Exception):
    pass


# A Phase 16 security review found `Concat.parts`/`Coalesce.options`/nested `Expression` fields
# have no depth or size limit — Pydantic would happily construct (and `evaluate()`/
# `referenced_columns()` would happily recurse through) an arbitrarily deep expression tree
# submitted in a mapping document, risking a `RecursionError` at validation time. 20 covers any
# expression a real mapping would ever need by a wide margin.
_MAX_EXPRESSION_DEPTH = 20


def check_expression_depth(expression: Expression, *, _depth: int = 1) -> None:
    """Call before `evaluate()`/`referenced_columns()` on any expression sourced from a mapping
    document — those two functions stay simple, unbounded recursion (matching the tree shape
    exactly) and rely on this separate, explicit check having already run."""

    if _depth > _MAX_EXPRESSION_DEPTH:
        raise ExpressionTooDeepError(
            f"Expression nesting exceeds the maximum depth of {_MAX_EXPRESSION_DEPTH}"
        )
    if isinstance(expression, Concat):
        for part in expression.parts:
            check_expression_depth(part, _depth=_depth + 1)
    elif isinstance(expression, Coalesce):
        for option in expression.options:
            check_expression_depth(option, _depth=_depth + 1)
    elif isinstance(expression, UpperCase | LowerCase | Trim):
        check_expression_depth(expression.value, _depth=_depth + 1)
    elif isinstance(expression, IfNullThenElse):
        check_expression_depth(expression.value, _depth=_depth + 1)
        check_expression_depth(expression.if_null, _depth=_depth + 1)


def evaluate(expression: Expression, *, row: dict[str, ScalarValue]) -> ScalarValue:
    """`row` is already-extracted, already-typed source column values for one record — this
    function never touches a database or any other I/O; it is pure data transformation."""

    if isinstance(expression, ColumnRef):
        if expression.column not in row:
            raise ExpressionEvaluationError(
                f"Unknown source column reference: {expression.column!r}"
            )
        return row[expression.column]

    if isinstance(expression, Literal_):
        return expression.value

    if isinstance(expression, Concat):
        pieces = [evaluate(part, row=row) for part in expression.parts]
        return expression.separator.join("" if p is None else str(p) for p in pieces)

    if isinstance(expression, Coalesce):
        for option in expression.options:
            value = evaluate(option, row=row)
            if value is not None:
                return value
        return None

    if isinstance(expression, UpperCase):
        value = evaluate(expression.value, row=row)
        return value.upper() if isinstance(value, str) else value

    if isinstance(expression, LowerCase):
        value = evaluate(expression.value, row=row)
        return value.lower() if isinstance(value, str) else value

    if isinstance(expression, Trim):
        value = evaluate(expression.value, row=row)
        return value.strip() if isinstance(value, str) else value

    if isinstance(expression, IfNullThenElse):
        value = evaluate(expression.value, row=row)
        return value if value is not None else evaluate(expression.if_null, row=row)

    raise ExpressionEvaluationError(f"Unsupported expression node: {expression!r}")


def referenced_columns(expression: Expression) -> set[str]:
    """Used by mapping validation to check every column a derived expression touches actually
    exists in the source table — same allowlist discipline as a direct column mapping."""

    if isinstance(expression, ColumnRef):
        return {expression.column}
    if isinstance(expression, Literal_):
        return set()
    if isinstance(expression, Concat):
        return {c for part in expression.parts for c in referenced_columns(part)}
    if isinstance(expression, Coalesce):
        return {c for option in expression.options for c in referenced_columns(option)}
    if isinstance(expression, UpperCase | LowerCase | Trim):
        return referenced_columns(expression.value)
    if isinstance(expression, IfNullThenElse):
        return referenced_columns(expression.value) | referenced_columns(expression.if_null)
    raise ExpressionEvaluationError(f"Unsupported expression node: {expression!r}")
