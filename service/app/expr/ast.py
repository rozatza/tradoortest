"""Expression AST and the canonical IR mapping.

Nodes are frozen. `result_type` is the inferred type of that node.
`to_ir` emits the signal-expression object from `schemas/strategy-ir-v1.schema.json`.
Indicator calls parse, but they are not IR expression operators.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import ExprError


class ExprType(Enum):
    """Inferred type of an expression node."""

    NUMERIC = "NUMERIC"
    BOOLEAN = "BOOLEAN"


@dataclass(frozen=True, slots=True)
class Span:
    """Source range. `end` is an exclusive offset."""

    offset: int
    end: int
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class Lit:
    value: bool | int | float
    result_type: ExprType
    span: Span

    def __post_init__(self) -> None:
        if isinstance(self.value, bool):
            expected = ExprType.BOOLEAN
        elif isinstance(self.value, int):
            expected = ExprType.NUMERIC
        elif isinstance(self.value, float) and math.isfinite(self.value):
            expected = ExprType.NUMERIC
        else:
            raise TypeError("literal value must be a bool or a finite int or float")
        if self.result_type is not expected:
            raise TypeError("literal result_type does not match its value")


@dataclass(frozen=True, slots=True)
class Ref:
    name: str
    result_type: ExprType
    span: Span


@dataclass(frozen=True, slots=True)
class Unary:
    op: str
    arg: Expr
    result_type: ExprType
    span: Span


@dataclass(frozen=True, slots=True)
class Binary:
    op: str
    left: Expr
    right: Expr
    result_type: ExprType
    span: Span


@dataclass(frozen=True, slots=True)
class Call:
    """`cross_above`, `cross_below`, `bars_since`, or `entry_price`."""

    op: str
    args: tuple[Expr, ...]
    result_type: ExprType
    span: Span


@dataclass(frozen=True, slots=True)
class IndicatorCall:
    """A numeric indicator invocation. `params` are in canonical parameter order."""

    name: str
    source: Expr
    params: tuple[tuple[str, Expr], ...]
    output: str | None
    result_type: ExprType
    span: Span


Expr = Lit | Ref | Unary | Binary | Call | IndicatorCall

_OP_TEXT = {
    "add": "+",
    "sub": "-",
    "mul": "*",
    "div": "/",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "eq": "==",
    "neq": "!=",
    "and": "AND",
    "or": "OR",
}

_CALL_TEXT = {
    "cross_above": "CROSS_ABOVE",
    "cross_below": "CROSS_BELOW",
    "bars_since": "BARS_SINCE",
    "entry_price": "ENTRY_PRICE",
}

# Higher binds tighter. Matches the grammar in docs/dsl-spec.md.
_PRECEDENCE = {
    "or": 1,
    "and": 2,
    "not": 3,
    "gt": 4,
    "gte": 4,
    "lt": 4,
    "lte": 4,
    "eq": 4,
    "neq": 4,
    "add": 5,
    "sub": 5,
    "mul": 6,
    "div": 6,
    "neg": 7,
}
_CMP = frozenset({"gt", "gte", "lt", "lte", "eq", "neq"})


def to_ir(node: Expr) -> dict[str, Any]:
    """Return the canonical IR object for a signal expression."""
    if isinstance(node, Lit):
        return {"op": "lit", "value": node.value}
    if isinstance(node, Ref):
        return {"op": "ref", "name": node.name}
    if isinstance(node, Unary):
        return {"op": node.op, "arg": to_ir(node.arg)}
    if isinstance(node, Binary):
        return {"op": node.op, "left": to_ir(node.left), "right": to_ir(node.right)}
    if isinstance(node, Call):
        return {"op": node.op, "args": [to_ir(arg) for arg in node.args]}
    if isinstance(node, IndicatorCall):
        raise ExprError(
            f"define {node.name} on a derived series line and reference that name",
            offset=node.span.offset,
            line=node.span.line,
            column=node.span.column,
            code="invalid_expr",
        )
    raise TypeError(f"unknown expression node {type(node).__name__}")


def format_expr(node: Expr) -> str:
    """Render an expression in the DSL spelling. The result parses back to the same IR."""
    return _format(node)


def _format(node: Expr) -> str:
    if isinstance(node, Lit):
        return _format_lit(node.value)
    if isinstance(node, Ref):
        return node.name
    if isinstance(node, Unary):
        inner = _paren(_format(node.arg), node.arg, _PRECEDENCE[node.op], right_assoc=True)
        if node.op == "neg":
            return f"-{inner}"
        if node.op == "not":
            return f"NOT {inner}"
        raise TypeError(f"unknown unary operator {node.op}")
    if isinstance(node, Binary):
        prec = _PRECEDENCE[node.op]
        left = _paren(_format(node.left), node.left, prec, is_right=False)
        right = _paren(_format(node.right), node.right, prec, is_right=True)
        return f"{left} {_OP_TEXT[node.op]} {right}"
    if isinstance(node, Call):
        args = ", ".join(_format(arg) for arg in node.args)
        return f"{_CALL_TEXT[node.op]}({args})"
    if isinstance(node, IndicatorCall):
        parts = [_format(node.source)]
        parts.extend(f"{name}={_format(value)}" for name, value in node.params)
        text = f"{node.name}({', '.join(parts)})"
        if node.output is not None:
            text = f"{text}.{node.output}"
        return text
    raise TypeError(f"unknown expression node {type(node).__name__}")


def _format_lit(value: bool | int | float) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = format(value, ".16g")
    if "e" not in text and "." not in text:
        text += ".0"
    return text


def _node_prec(node: Expr) -> int:
    if isinstance(node, (Unary, Binary)):
        return _PRECEDENCE[node.op]
    return 8


def _paren(
    text: str,
    node: Expr,
    parent_prec: int,
    *,
    is_right: bool = False,
    right_assoc: bool = False,
) -> str:
    child_prec = _node_prec(node)
    if child_prec < parent_prec:
        return f"({text})"
    if child_prec == parent_prec and is_right and not right_assoc:
        return f"({text})"
    if (
        child_prec == parent_prec
        and isinstance(node, Binary)
        and node.op in _CMP
        and parent_prec == _PRECEDENCE["gt"]
    ):
        return f"({text})"
    return text
