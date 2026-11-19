"""Recursive-descent parser for the strategy expression mini-language.

The grammar and precedence follow `docs/dsl-spec.md`. Bare identifiers are
typed NUMERIC. Whether a name is declared is a later semantic check.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .ast import (
    Binary,
    Call,
    Expr,
    ExprType,
    IndicatorCall,
    Lit,
    Ref,
    Span,
    Unary,
)
from .errors import ExprError
from .lexer import Token, TokenKind, tokenize


def _is_ident_text(name: str) -> bool:
    """True when `name` matches `^[a-z][a-z0-9_]{0,63}$`."""
    if not name or len(name) > 64:
        return False
    first, rest = name[0], name[1:]
    if not (first.isascii() and first.islower()):
        return False
    return all(ch.isascii() and (ch.islower() or ch.isdigit() or ch == "_") for ch in rest)


_CMP = {
    TokenKind.GT: "gt",
    TokenKind.GTE: "gte",
    TokenKind.LT: "lt",
    TokenKind.LTE: "lte",
    TokenKind.EQ: "eq",
    TokenKind.NEQ: "neq",
}
_ADD = {TokenKind.PLUS: "add", TokenKind.MINUS: "sub"}
_MUL = {TokenKind.STAR: "mul", TokenKind.SLASH: "div"}

_OP_LABEL = {
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


@dataclass(frozen=True, slots=True)
class _Function:
    op: str
    display: str
    arity: int
    result: ExprType
    params: tuple[ExprType, ...]


@dataclass(frozen=True, slots=True)
class _Indicator:
    name: str
    params: tuple[str, ...]
    outputs: tuple[str, ...] | None


_FUNCTIONS = {
    "cross_above": _Function(
        "cross_above", "CROSS_ABOVE", 2, ExprType.BOOLEAN, (ExprType.NUMERIC, ExprType.NUMERIC)
    ),
    "cross_below": _Function(
        "cross_below", "CROSS_BELOW", 2, ExprType.BOOLEAN, (ExprType.NUMERIC, ExprType.NUMERIC)
    ),
    "bars_since": _Function("bars_since", "BARS_SINCE", 1, ExprType.NUMERIC, (ExprType.BOOLEAN,)),
    "entry_price": _Function("entry_price", "ENTRY_PRICE", 0, ExprType.NUMERIC, ()),
}

_INDICATORS = {
    "sma": _Indicator("sma", ("period",), None),
    "ema": _Indicator("ema", ("period",), None),
    "atr": _Indicator("atr", ("period",), None),
    "rsi": _Indicator("rsi", ("period",), None),
    "macd": _Indicator("macd", ("fast", "slow", "signal"), ("macd", "signal", "histogram")),
    "bollinger": _Indicator("bollinger", ("period", "stddev"), ("lower", "middle", "upper")),
    "stochastic": _Indicator("stochastic", ("k_period", "d_period"), ("k", "d")),
}

_ALIASES = {"bb": "bollinger"}


def parse(source: str, *, expected: ExprType | None = None) -> Expr:
    """Parse an expression. `expected` checks the root type when it is set."""
    if not isinstance(source, str):
        raise TypeError("expression must be a string")
    tokens = tokenize(source)
    if tokens[0].kind is TokenKind.EOF:
        eof = tokens[0]
        raise ExprError("empty input", offset=eof.offset, line=eof.line, column=eof.column)
    parser = _Parser(tokens)
    node = parser.parse_or()
    current = parser.current
    if current.kind is not TokenKind.EOF:
        raise _at(current, f"trailing input: unexpected token '{current.lexeme}'")
    if expected is not None and node.result_type is not expected:
        context = "condition" if expected is ExprType.BOOLEAN else "numeric expression"
        raise _at_span(
            node.span,
            f"type error: {context} expects {expected.value}, got {node.result_type.value}",
        )
    return node


def parse_condition(source: str) -> Expr:
    """Parse an expression that must have type BOOLEAN."""
    return parse(source, expected=ExprType.BOOLEAN)


def parse_numeric(source: str) -> Expr:
    """Parse an expression that must have type NUMERIC."""
    return parse(source, expected=ExprType.NUMERIC)


def from_ir(tree: Mapping[str, Any]) -> Expr:
    """Build a typed AST from a canonical IR expression object."""
    if not isinstance(tree, Mapping):
        raise TypeError("IR expression must be an object")
    return _from_ir(tree)


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current
        if token.kind is not TokenKind.EOF:
            self.index += 1
        return token

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.current.kind is TokenKind.OR:
            op = self.advance()
            right = self.parse_and()
            left = _bool_binary("or", left, right, op)
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.current.kind is TokenKind.AND:
            op = self.advance()
            right = self.parse_not()
            left = _bool_binary("and", left, right, op)
        return left

    def parse_not(self) -> Expr:
        if self.current.kind is not TokenKind.NOT:
            return self.parse_cmp()
        op = self.advance()
        arg = self.parse_not()
        _expect(arg, ExprType.BOOLEAN, "NOT")
        return Unary("not", arg, ExprType.BOOLEAN, _cover(op, arg.span.end))

    def parse_cmp(self) -> Expr:
        left = self.parse_add()
        if self.current.kind not in _CMP:
            return left
        op = self.advance()
        right = self.parse_add()
        if self.current.kind in _CMP:
            nxt = self.current
            raise _at(nxt, f"unexpected token '{nxt.lexeme}': chained comparisons are not allowed")
        return _numeric_binary(_CMP[op.kind], left, right, op, ExprType.BOOLEAN)

    def parse_add(self) -> Expr:
        left = self.parse_mul()
        while self.current.kind in _ADD:
            op = self.advance()
            right = self.parse_mul()
            left = _numeric_binary(_ADD[op.kind], left, right, op, ExprType.NUMERIC)
        return left

    def parse_mul(self) -> Expr:
        left = self.parse_unary()
        while self.current.kind in _MUL:
            op = self.advance()
            right = self.parse_unary()
            left = _numeric_binary(_MUL[op.kind], left, right, op, ExprType.NUMERIC)
        return left

    def parse_unary(self) -> Expr:
        if self.current.kind is not TokenKind.MINUS:
            return self.parse_primary()
        op = self.advance()
        arg = self.parse_unary()
        _expect(arg, ExprType.NUMERIC, "unary minus")
        return Unary("neg", arg, ExprType.NUMERIC, _cover(op, arg.span.end))

    def parse_primary(self) -> Expr:
        token = self.current
        if token.kind is TokenKind.NUMBER:
            self.advance()
            if token.value is None:
                raise _at(token, "bad number")
            return Lit(token.value, ExprType.NUMERIC, _span(token))
        if token.kind is TokenKind.TRUE:
            self.advance()
            return Lit(True, ExprType.BOOLEAN, _span(token))
        if token.kind is TokenKind.FALSE:
            self.advance()
            return Lit(False, ExprType.BOOLEAN, _span(token))
        if token.kind is TokenKind.IDENT:
            return self.parse_ident_or_call()
        if token.kind is TokenKind.LPAREN:
            return self.parse_group()
        if token.kind is TokenKind.EOF:
            raise _at(token, "unexpected token (end of input)")
        raise _at(token, f"unexpected token '{token.lexeme}'")

    def parse_group(self) -> Expr:
        opening = self.advance()
        if self.current.kind is TokenKind.RPAREN:
            raise _at(self.current, "unexpected token ')'")
        node = self.parse_or()
        if self.current.kind is not TokenKind.RPAREN:
            if self.current.kind is TokenKind.EOF:
                raise _at(opening, "unterminated paren")
            raise _at(self.current, f"unexpected token '{self.current.lexeme}'")
        closing = self.advance()
        return _with_span(node, Span(opening.offset, closing.end, opening.line, opening.column))

    def parse_ident_or_call(self) -> Expr:
        name = self.advance()
        if self.current.kind is not TokenKind.LPAREN:
            return _ref(name)
        opening = self.advance()
        args, keywords = self.parse_args()
        if self.current.kind is not TokenKind.RPAREN:
            if self.current.kind is TokenKind.EOF:
                raise _at(opening, "unterminated paren")
            raise _at(self.current, f"unexpected token '{self.current.lexeme}'")
        closing = self.advance()
        end = closing.end
        output: Token | None = None
        if self.current.kind is TokenKind.DOT:
            self.advance()
            if self.current.kind is TokenKind.EOF:
                raise _at(self.current, "unexpected token (end of input)")
            if self.current.kind is not TokenKind.IDENT:
                raise _at(self.current, f"unexpected token '{self.current.lexeme}'")
            output = self.advance()
            end = output.end
        return _finish_call(name, args, keywords, output, end)

    def parse_args(self) -> tuple[list[Expr], list[tuple[Token, Expr]]]:
        args: list[Expr] = []
        keywords: list[tuple[Token, Expr]] = []
        if self.current.kind in (TokenKind.RPAREN, TokenKind.EOF):
            return args, keywords
        while True:
            if self._at_keyword():
                key = self.advance()
                self.advance()
                canonical = key.lexeme.lower()
                if any(seen.lexeme.lower() == canonical for seen, _ in keywords):
                    raise _at(key, f"duplicate argument '{canonical}'")
                if not _is_ident_text(canonical):
                    raise _at(key, f"invalid identifier '{key.lexeme}'")
                keywords.append((key, self.parse_or()))
            else:
                if keywords:
                    raise _unexpected(
                        self.current,
                        "positional argument follows a keyword argument",
                    )
                args.append(self.parse_or())
            if self.current.kind is not TokenKind.COMMA:
                break
            comma = self.advance()
            if self.current.kind in (TokenKind.RPAREN, TokenKind.EOF):
                raise _at(comma, "unexpected token ','")
        return args, keywords

    def _at_keyword(self) -> bool:
        if self.current.kind is not TokenKind.IDENT:
            return False
        nxt = self.tokens[self.index + 1]
        return nxt.kind is TokenKind.ASSIGN


def _finish_call(
    name: Token,
    args: list[Expr],
    keywords: list[tuple[Token, Expr]],
    output: Token | None,
    end: int,
) -> Expr:
    key = _ALIASES.get(name.lexeme.lower(), name.lexeme.lower())
    span = Span(name.offset, end, name.line, name.column)
    function = _FUNCTIONS.get(key)
    if function is not None:
        if keywords:
            raise _at(keywords[0][0], f"keyword arguments are not allowed in {function.display}")
        if output is not None:
            raise _at(output, f"{function.display} does not take an output field")
        if len(args) != function.arity:
            noun = "argument" if function.arity == 1 else "arguments"
            raise _at(
                name,
                f"wrong arity: {function.display} expects {function.arity} {noun}, got {len(args)}",
            )
        for index, (arg, expected) in enumerate(zip(args, function.params, strict=True), start=1):
            _expect(arg, expected, f"{function.display} argument {index}")
        return Call(function.op, tuple(args), function.result, span)
    indicator = _INDICATORS.get(key)
    if indicator is None:
        raise _at(name, f"unknown function '{name.lexeme}'")
    return _finish_indicator(indicator, args, keywords, output, span)


def _finish_indicator(
    indicator: _Indicator,
    args: list[Expr],
    keywords: list[tuple[Token, Expr]],
    output: Token | None,
    span: Span,
) -> IndicatorCall:
    expected = 1 + len(indicator.params)
    if len(args) > expected:
        raise _at_span(
            span,
            f"wrong arity: {indicator.name} expects {expected} arguments, got {len(args)}",
        )

    kw = {token.lexeme.lower(): (token, value) for token, value in keywords}
    allowed = {"source", *indicator.params}
    for key, (token, _value) in kw.items():
        if key not in allowed:
            raise _at(token, f"unknown parameter '{key}' for {indicator.name}")

    source: Expr | None = None
    pos = 0
    if args:
        source = args[0]
        pos = 1
    if source is None:
        found = kw.pop("source", None)
        if found is None:
            listed = ", ".join(("source", *indicator.params))
            raise _at_span(
                span,
                f"wrong arity: {indicator.name} expects arguments ({listed}), missing source",
            )
        source = found[1]
    elif "source" in kw:
        raise _at(kw["source"][0], "duplicate argument 'source'")
    _expect(source, ExprType.NUMERIC, f"{indicator.name} argument source")

    bound: list[tuple[str, Expr]] = []
    for pname in indicator.params:
        if pos < len(args):
            value = args[pos]
            pos += 1
            if pname in kw:
                raise _at(kw[pname][0], f"duplicate argument '{pname}'")
        elif pname in kw:
            value = kw.pop(pname)[1]
        else:
            listed = ", ".join(("source", *indicator.params))
            raise _at_span(
                span,
                f"wrong arity: {indicator.name} expects arguments ({listed}), missing {pname}",
            )
        _expect(value, ExprType.NUMERIC, f"{indicator.name} argument {pname}")
        bound.append((pname, value))
    if pos != len(args):
        raise _at_span(
            span,
            f"wrong arity: {indicator.name} expects {expected} arguments, got {len(args)}",
        )

    output_name = _check_output(indicator, output, span)
    if output is not None:
        span = Span(span.offset, output.end, span.line, span.column)
    return IndicatorCall(indicator.name, source, tuple(bound), output_name, ExprType.NUMERIC, span)


def _check_output(indicator: _Indicator, output: Token | None, span: Span) -> str | None:
    if indicator.outputs is None:
        if output is not None:
            raise _at(output, f"{indicator.name} does not take an output field")
        return None
    if output is None:
        fields = ", ".join(f".{name}" for name in indicator.outputs)
        raise _at_span(span, f"{indicator.name} requires an output field ({fields})")
    folded = output.lexeme.lower()
    if folded not in indicator.outputs:
        raise _at(output, f"unknown output '{output.lexeme}' for {indicator.name}")
    return folded


def _ref(token: Token) -> Ref:
    key = _ALIASES.get(token.lexeme.lower(), token.lexeme.lower())
    known = key in _FUNCTIONS or key in _INDICATORS
    if _is_ident_text(token.lexeme):
        return Ref(token.lexeme, ExprType.NUMERIC, _span(token))
    if known:
        raise _at(token, f"unexpected token '{token.lexeme}': expected '('")
    raise _at(token, f"invalid identifier '{token.lexeme}'")


def _bool_binary(op: str, left: Expr, right: Expr, op_token: Token) -> Binary:
    _expect(left, ExprType.BOOLEAN, _OP_LABEL[op])
    _expect(right, ExprType.BOOLEAN, _OP_LABEL[op])
    return Binary(op, left, right, ExprType.BOOLEAN, _between(left, right, op_token))


def _numeric_binary(op: str, left: Expr, right: Expr, op_token: Token, result: ExprType) -> Binary:
    label = _OP_LABEL[op]
    _expect(left, ExprType.NUMERIC, label)
    _expect(right, ExprType.NUMERIC, label)
    return Binary(op, left, right, result, _between(left, right, op_token))


def _expect(node: Expr, expected: ExprType, context: str) -> None:
    if node.result_type is not expected:
        raise _at_span(
            node.span,
            f"type error: {context} expects {expected.value}, got {node.result_type.value}",
        )


def _from_ir(tree: Mapping[str, Any]) -> Expr:
    op = tree.get("op")
    span = Span(0, 0, 1, 1)
    if op == "lit":
        return _lit_from_ir(tree.get("value"), span)
    if op == "ref":
        name = tree.get("name")
        if not isinstance(name, str) or not _is_ident_text(name):
            raise ValueError("IR ref name is not an identifier")
        return Ref(name, ExprType.NUMERIC, span)
    if op in ("neg", "not"):
        arg = _child(tree, "arg")
        if op == "neg":
            _expect(arg, ExprType.NUMERIC, "unary minus")
            return Unary("neg", arg, ExprType.NUMERIC, span)
        _expect(arg, ExprType.BOOLEAN, "NOT")
        return Unary("not", arg, ExprType.BOOLEAN, span)
    if op in _OP_LABEL and op not in ("neg", "not"):
        left = _child(tree, "left")
        right = _child(tree, "right")
        token = Token(TokenKind.EOF, "", 0, 0, 1, 1)
        if op in ("and", "or"):
            return _bool_binary(op, left, right, token)
        result = ExprType.BOOLEAN if op in _CMP.values() else ExprType.NUMERIC
        return _numeric_binary(op, left, right, token, result)
    if op in _FUNCTIONS:
        raw_args = tree.get("args")
        if not isinstance(raw_args, list):
            raise ValueError(f"IR call {op} is missing args")
        args = [_from_ir(arg) if isinstance(arg, Mapping) else _bad_child() for arg in raw_args]
        name = Token(TokenKind.IDENT, _FUNCTIONS[op].display, 0, 0, 1, 1)
        node = _finish_call(name, args, [], None, 0)
        return node
    raise ValueError(f"IR expression op {op!r} is not a canonical operator")


def _lit_from_ir(value: object, span: Span) -> Lit:
    if isinstance(value, bool):
        return Lit(value, ExprType.BOOLEAN, span)
    if isinstance(value, int):
        return Lit(value, ExprType.NUMERIC, span)
    if isinstance(value, float) and math.isfinite(value):
        return Lit(value, ExprType.NUMERIC, span)
    raise ValueError("IR lit value must be a bool or a finite number")


def _child(tree: Mapping[str, Any], key: str) -> Expr:
    value = tree.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"IR expression is missing {key}")
    return _from_ir(value)


def _bad_child() -> Expr:
    raise ValueError("IR call argument must be an object")


def _span(token: Token) -> Span:
    return Span(token.offset, token.end, token.line, token.column)


def _cover(token: Token, end: int) -> Span:
    return Span(token.offset, end, token.line, token.column)


def _between(left: Expr, right: Expr, _op: Token) -> Span:
    return Span(left.span.offset, right.span.end, left.span.line, left.span.column)


def _unexpected(token: Token, detail: str) -> ExprError:
    if token.kind is TokenKind.EOF:
        return _at(token, "unexpected token (end of input)")
    return _at(token, f"unexpected token '{token.lexeme}': {detail}")


def _at(token: Token, message: str) -> ExprError:
    return ExprError(message, offset=token.offset, line=token.line, column=token.column)


def _at_span(span: Span, message: str) -> ExprError:
    return ExprError(message, offset=span.offset, line=span.line, column=span.column)


def _with_span(node: Expr, span: Span) -> Expr:
    if isinstance(node, Lit):
        return Lit(node.value, node.result_type, span)
    if isinstance(node, Ref):
        return Ref(node.name, node.result_type, span)
    if isinstance(node, Unary):
        return Unary(node.op, node.arg, node.result_type, span)
    if isinstance(node, Binary):
        return Binary(node.op, node.left, node.right, node.result_type, span)
    if isinstance(node, Call):
        return Call(node.op, node.args, node.result_type, span)
    return IndicatorCall(node.name, node.source, node.params, node.output, node.result_type, span)
