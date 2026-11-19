"""Recursive-descent parser for the strategy expression mini-language.

The grammar and precedence follow `docs/dsl-spec.md`. Without a name map, a
bare identifier is NUMERIC. Pass `names` to resolve series (NUMERIC) and
signals (BOOLEAN) while parsing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from contextvars import ContextVar
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

# Recursive productions (grouping parentheses, calls, NOT, unary minus) stop
# at this depth. The same limit bounds `from_ir`, which recurses on every
# nested IR node. 80 sits between the required 64 and 100.
MAX_EXPR_DEPTH = 80

# Source length, in Unicode code points. Checked before tokenization.
MAX_EXPR_SOURCE_LENGTH = 10_000

# Built-in bar series. A name map may override one; otherwise these are NUMERIC.
_BUILTIN_SERIES = frozenset({"open", "high", "low", "close", "volume"})

# Set by `from_ir` so nested helpers can attach a JSONPath without a source span.
_ir_path: ContextVar[str | None] = ContextVar("ir_path", default=None)


def parse(
    source: str,
    *,
    expected: ExprType | None = None,
    names: Mapping[str, ExprType] | None = None,
) -> Expr:
    """Parse an expression. `expected` checks the root type when it is set.

    `names` maps identifiers that are already in scope to their types. Built-in
    series stay NUMERIC unless the map says otherwise. A name that is in
    neither the map nor the built-in set is an `unknown_series` error. Omit
    `names` to treat every bare identifier as NUMERIC.
    """
    if not isinstance(source, str):
        raise TypeError("expression must be a string")
    if len(source) > MAX_EXPR_SOURCE_LENGTH:
        offset = MAX_EXPR_SOURCE_LENGTH
        line, column = _source_position(source, offset)
        raise ExprError(
            f"expression exceeds length limit {MAX_EXPR_SOURCE_LENGTH}",
            offset=offset,
            line=line,
            column=column,
            code="expression_too_long",
        )
    tokens = tokenize(source)
    if tokens[0].kind is TokenKind.EOF:
        eof = tokens[0]
        raise ExprError("empty input", offset=eof.offset, line=eof.line, column=eof.column)
    parser = _Parser(tokens, names)
    node = parser.parse_or()
    current = parser.current
    if current.kind is not TokenKind.EOF:
        raise _at(current, f"trailing input: unexpected token '{current.lexeme}'")
    if expected is not None and node.result_type is not expected:
        context = "condition" if expected is ExprType.BOOLEAN else "numeric expression"
        raise _at_span(
            node.span,
            f"type error: {context} expects {expected.value}, got {node.result_type.value}",
            code="type_error",
        )
    return node


def parse_condition(source: str, *, names: Mapping[str, ExprType] | None = None) -> Expr:
    """Parse an expression that must have type BOOLEAN."""
    return parse(source, expected=ExprType.BOOLEAN, names=names)


def parse_numeric(source: str, *, names: Mapping[str, ExprType] | None = None) -> Expr:
    """Parse an expression that must have type NUMERIC."""
    return parse(source, expected=ExprType.NUMERIC, names=names)


def from_ir(
    tree: Mapping[str, Any],
    *,
    names: Mapping[str, ExprType] | None = None,
    path: str = "$",
) -> Expr:
    """Build a typed AST from a canonical IR expression object.

    `names` is the same map `parse` accepts. `path` is the JSONPath of `tree`
    and is copied onto errors (`$.signals[1].expr.args[0]`).
    """
    if not isinstance(tree, Mapping):
        raise ExprError(
            "IR expression must be an object",
            offset=0,
            line=1,
            column=1,
            code="invalid_expr",
            path=path,
        )
    return _from_ir(tree, names=names, path=path, depth=0)


class _Parser:
    def __init__(self, tokens: list[Token], names: Mapping[str, ExprType] | None) -> None:
        self.tokens = tokens
        self.names = names
        self.index = 0
        self.depth = 0

    def _enter(self, token: Token) -> None:
        self.depth += 1
        if self.depth > MAX_EXPR_DEPTH:
            raise _at(
                token,
                f"expression nested too deeply (limit {MAX_EXPR_DEPTH})",
                code="expr_too_deep",
            )

    def _leave(self) -> None:
        self.depth -= 1

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
        self._enter(op)
        arg = self.parse_not()
        self._leave()
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
        self._enter(op)
        arg = self.parse_unary()
        self._leave()
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
        self._enter(opening)
        if self.current.kind is TokenKind.RPAREN:
            raise _at(self.current, "unexpected token ')'")
        node = self.parse_or()
        self._leave()
        if self.current.kind is not TokenKind.RPAREN:
            if self.current.kind is TokenKind.EOF:
                raise _at(opening, "unterminated paren")
            raise _at(self.current, f"unexpected token '{self.current.lexeme}'")
        closing = self.advance()
        return _with_span(node, Span(opening.offset, closing.end, opening.line, opening.column))

    def parse_ident_or_call(self) -> Expr:
        name = self.advance()
        if self.current.kind is not TokenKind.LPAREN:
            return _ref(name, self.names)
        self._enter(name)
        try:
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
        finally:
            self._leave()

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
        for index, (arg, expected) in enumerate(zip(args, function.params, strict=True)):
            _expect(
                arg,
                expected,
                f"{function.display} argument {index + 1}",
                path=_indexed_path(f"args[{index}]"),
            )
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


def _ref(token: Token, names: Mapping[str, ExprType] | None) -> Ref:
    key = _ALIASES.get(token.lexeme.lower(), token.lexeme.lower())
    known = key in _FUNCTIONS or key in _INDICATORS
    if not _is_ident_text(token.lexeme):
        if known:
            raise _at(token, f"unexpected token '{token.lexeme}': expected '('")
        raise _at(token, f"invalid identifier '{token.lexeme}'")
    resolved = _lookup_name(token.lexeme, names)
    if resolved is None:
        raise _at(
            token,
            f"unknown series reference '{token.lexeme}'",
            code="unknown_series",
            path=_indexed_path("name"),
        )
    return Ref(token.lexeme, resolved, _span(token))


def _lookup_name(name: str, names: Mapping[str, ExprType] | None) -> ExprType | None:
    """Type of `name`, or None when a name map is active and the name is unknown.

    With no map, every identifier is NUMERIC. Built-in series are NUMERIC
    unless the map overrides them.
    """
    if names is None:
        return ExprType.NUMERIC
    found = names.get(name)
    if found is not None:
        return found
    if name in _BUILTIN_SERIES:
        return ExprType.NUMERIC
    return None


def _bool_binary(
    op: str,
    left: Expr,
    right: Expr,
    op_token: Token,
    *,
    left_path: str | None = None,
    right_path: str | None = None,
) -> Binary:
    _expect(left, ExprType.BOOLEAN, _OP_LABEL[op], path=left_path)
    _expect(right, ExprType.BOOLEAN, _OP_LABEL[op], path=right_path)
    return Binary(op, left, right, ExprType.BOOLEAN, _between(left, right, op_token))


def _numeric_binary(
    op: str,
    left: Expr,
    right: Expr,
    op_token: Token,
    result: ExprType,
    *,
    left_path: str | None = None,
    right_path: str | None = None,
) -> Binary:
    label = _OP_LABEL[op]
    _expect(left, ExprType.NUMERIC, label, path=left_path)
    _expect(right, ExprType.NUMERIC, label, path=right_path)
    return Binary(op, left, right, result, _between(left, right, op_token))


def _expect(node: Expr, expected: ExprType, context: str, *, path: str | None = None) -> None:
    if node.result_type is not expected:
        raise _at_span(
            node.span,
            f"type error: {context} expects {expected.value}, got {node.result_type.value}",
            code="type_error",
            path=path,
        )


def _from_ir(
    tree: Mapping[str, Any],
    *,
    names: Mapping[str, ExprType] | None,
    path: str,
    depth: int,
) -> Expr:
    if depth > MAX_EXPR_DEPTH:
        raise ExprError(
            f"expression nested too deeply (limit {MAX_EXPR_DEPTH})",
            offset=0,
            line=1,
            column=1,
            code="expr_too_deep",
            path=path,
        )
    token = _ir_path.set(path)
    try:
        return _from_ir_node(tree, names=names, path=path, depth=depth)
    finally:
        _ir_path.reset(token)


def _from_ir_node(
    tree: Mapping[str, Any],
    *,
    names: Mapping[str, ExprType] | None,
    path: str,
    depth: int,
) -> Expr:
    op = tree.get("op")
    span = Span(0, 0, 1, 1)
    if op == "lit":
        return _lit_from_ir(tree.get("value"), span, path)
    if op == "ref":
        name = tree.get("name")
        if not isinstance(name, str) or not _is_ident_text(name):
            raise ExprError(
                "IR ref name is not an identifier",
                offset=0,
                line=1,
                column=1,
                code="invalid_expr",
                path=f"{path}.name",
            )
        resolved = _lookup_name(name, names)
        if resolved is None:
            raise ExprError(
                f"unknown series reference '{name}'",
                offset=0,
                line=1,
                column=1,
                code="unknown_series",
                path=f"{path}.name",
            )
        return Ref(name, resolved, span)
    if op in ("neg", "not"):
        arg = _child(tree, "arg", names=names, path=path, depth=depth)
        if op == "neg":
            _expect(arg, ExprType.NUMERIC, "unary minus", path=f"{path}.arg")
            return Unary("neg", arg, ExprType.NUMERIC, span)
        _expect(arg, ExprType.BOOLEAN, "NOT", path=f"{path}.arg")
        return Unary("not", arg, ExprType.BOOLEAN, span)
    if op in _OP_LABEL and op not in ("neg", "not"):
        left = _child(tree, "left", names=names, path=path, depth=depth)
        right = _child(tree, "right", names=names, path=path, depth=depth)
        marker = Token(TokenKind.EOF, "", 0, 0, 1, 1)
        if op in ("and", "or"):
            return _bool_binary(
                op, left, right, marker, left_path=f"{path}.left", right_path=f"{path}.right"
            )
        result = ExprType.BOOLEAN if op in _CMP.values() else ExprType.NUMERIC
        return _numeric_binary(
            op,
            left,
            right,
            marker,
            result,
            left_path=f"{path}.left",
            right_path=f"{path}.right",
        )
    if op in _FUNCTIONS:
        raw_args = tree.get("args")
        if not isinstance(raw_args, list):
            raise ExprError(
                f"IR call {op} is missing args",
                offset=0,
                line=1,
                column=1,
                code="invalid_expr",
                path=f"{path}.args",
            )
        args: list[Expr] = []
        for index, arg in enumerate(raw_args):
            arg_path = f"{path}.args[{index}]"
            if not isinstance(arg, Mapping):
                raise ExprError(
                    "IR call argument must be an object",
                    offset=0,
                    line=1,
                    column=1,
                    code="invalid_expr",
                    path=arg_path,
                )
            args.append(_from_ir(arg, names=names, path=arg_path, depth=depth + 1))
        name = Token(TokenKind.IDENT, _FUNCTIONS[op].display, 0, 0, 1, 1)
        return _finish_call(name, args, [], None, 0)
    raise ExprError(
        f"IR expression op {op!r} is not a canonical operator",
        offset=0,
        line=1,
        column=1,
        code="invalid_expr",
        path=path,
    )


def _lit_from_ir(value: object, span: Span, path: str) -> Lit:
    if isinstance(value, bool):
        return Lit(value, ExprType.BOOLEAN, span)
    if isinstance(value, int):
        return Lit(value, ExprType.NUMERIC, span)
    if isinstance(value, float) and math.isfinite(value):
        return Lit(value, ExprType.NUMERIC, span)
    raise ExprError(
        "IR lit value must be a bool or a finite number",
        offset=0,
        line=1,
        column=1,
        code="invalid_expr",
        path=f"{path}.value",
    )


def _child(
    tree: Mapping[str, Any],
    key: str,
    *,
    names: Mapping[str, ExprType] | None,
    path: str,
    depth: int,
) -> Expr:
    value = tree.get(key)
    child_path = f"{path}.{key}"
    if not isinstance(value, Mapping):
        raise ExprError(
            f"IR expression is missing {key}",
            offset=0,
            line=1,
            column=1,
            code="invalid_expr",
            path=child_path,
        )
    return _from_ir(value, names=names, path=child_path, depth=depth + 1)


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


def _indexed_path(suffix: str) -> str | None:
    base = _ir_path.get()
    if base is None:
        return None
    return f"{base}.{suffix}"


def _source_position(source: str, offset: int) -> tuple[int, int]:
    line = 1
    column = 1
    for index, ch in enumerate(source):
        if index == offset:
            return line, column
        if ch == "\n":
            line += 1
            column = 1
        elif ch == "\r":
            if index + 1 < len(source) and source[index + 1] == "\n":
                continue
            line += 1
            column = 1
        else:
            column += 1
    return line, column


def _at(
    token: Token,
    message: str,
    *,
    code: str | None = None,
    path: str | None = None,
) -> ExprError:
    return ExprError(
        message,
        offset=token.offset,
        line=token.line,
        column=token.column,
        code=code,
        path=path,
    )


def _at_span(
    span: Span,
    message: str,
    *,
    code: str | None = None,
    path: str | None = None,
) -> ExprError:
    return ExprError(
        message,
        offset=span.offset,
        line=span.line,
        column=span.column,
        code=code,
        path=path,
    )


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
