"""Parse a strategy DSL file into canonical IR v1.

Expression text is handed to `app.expr` with the names declared so far.
Series are NUMERIC. Signals are BOOLEAN. Built-in series are NUMERIC.
Document-level semantic checks (bar size, sizing, entry rules, duplicate
symbols) stay in `validate_ir`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.dsl.errors import StrategyError
from app.expr import (
    MAX_EXPR_DEPTH,
    MAX_NUMBER_LITERAL_DIGITS,
    Binary,
    Call,
    Expr,
    ExprError,
    ExprType,
    IndicatorCall,
    Lit,
    Ref,
    Unary,
    parse,
    parse_condition,
    to_ir,
)
from app.ir.canonical import canonicalize
from app.ir.constants import BUILTIN_SERIES, IR_VERSION
from app.ir.depth import lone_surrogate_at

_IDENT = r"[a-z][a-z0-9_]{0,63}"
_SERIES = re.compile(rf"^series\s+({_IDENT})\s*=\s*(.+)$")
_SIGNAL = re.compile(rf"^signal\s+({_IDENT})\s*=\s*(.+)$")
_STRATEGY = re.compile(rf"^strategy\s+({_IDENT})\s*:$")
_ASSET = re.compile(rf"^asset\s+({_IDENT})\s*:$")
_ACTION = re.compile(rf"^action\s+({_IDENT})\s*:$")
_RULE = re.compile(rf"^rule\s+({_IDENT})\s*:$")
_WHEN = re.compile(rf"^when\s+({_IDENT})\s+do\s+({_IDENT})$")
_SECTION = re.compile(r"^(engine|capital|costs|portfolio)\s*:$")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_TOKEN = re.compile(r"^[A-Za-z0-9_]+$")
_SIZING = re.compile(r"^(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)%\s+(equity|position)$")
_PERCENT = re.compile(r"^(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)%$")


@dataclass(frozen=True, slots=True)
class _Line:
    indent: int
    text: str
    offset: int
    line: int
    column: int


class _StrategyParser:
    def __init__(self, source: str, lines: list[_Line]) -> None:
        self.source = source
        self.lines = lines
        self.names: dict[str, ExprType] = {name: ExprType.NUMERIC for name in BUILTIN_SERIES}
        self.bound: set[str] = set()
        self.sections: set[str] = set()
        self.strategy: dict[str, Any] | None = None
        self.engine: dict[str, Any] | None = None
        self.capital: dict[str, Any] | None = None
        self.costs: dict[str, Any] | None = None
        self.assets: list[dict[str, Any]] = []
        self.series: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.rules: list[dict[str, Any]] = []
        self.portfolio: dict[str, Any] | None = None
        self.asset_ids: set[str] = set()
        self.action_ids: set[str] = set()
        self.rule_ids: set[str] = set()

    def parse(self) -> dict[str, Any]:
        index = 0
        while index < len(self.lines):
            line = self.lines[index]
            if line.indent != 0:
                raise self._error(line, "unexpected indent", "syntax")
            index = self._statement(index)
        self._require_sections()
        return {
            "ir_version": IR_VERSION,
            "strategy": self.strategy,
            "engine": self.engine,
            "capital": self.capital,
            "costs": self.costs,
            "assets": self.assets,
            "derived_series": self.series,
            "signals": self.signals,
            "actions": self.actions,
            "rules": self.rules,
            "portfolio": self.portfolio,
        }

    def _statement(self, index: int) -> int:
        line = self.lines[index]
        text = line.text
        matched = _STRATEGY.fullmatch(text)
        if matched:
            self._once("strategy", line)
            body, index = self._block(index)
            self.strategy = self._parse_strategy(matched.group(1), line, body)
            return index
        matched = _SECTION.fullmatch(text)
        if matched:
            name = matched.group(1)
            self._once(name, line)
            body, index = self._block(index)
            parsed = self._parse_section(name, line, body)
            setattr(self, name, parsed)
            return index
        matched = _ASSET.fullmatch(text)
        if matched:
            body, index = self._block(index)
            self.assets.append(self._parse_asset(matched.group(1), line, body))
            return index
        matched = _ACTION.fullmatch(text)
        if matched:
            body, index = self._block(index)
            self.actions.append(self._parse_action(matched.group(1), line, body))
            return index
        matched = _RULE.fullmatch(text)
        if matched:
            body, index = self._block(index)
            self.rules.append(self._parse_rule(matched.group(1), line, body))
            return index
        matched = _SERIES.fullmatch(text)
        if matched:
            self.series.append(self._parse_series(matched, line))
            return index + 1
        matched = _SIGNAL.fullmatch(text)
        if matched:
            self.signals.append(self._parse_signal(matched, line))
            return index + 1
        raise self._error(line, f"unexpected statement '{text}'", "syntax")

    def _block(self, index: int) -> tuple[list[_Line], int]:
        body: list[_Line] = []
        index += 1
        while index < len(self.lines) and self.lines[index].indent > 0:
            body.append(self.lines[index])
            index += 1
        return body, index

    def _once(self, name: str, line: _Line) -> None:
        if name in self.sections:
            raise self._error(line, f"duplicate section '{name}'", "duplicate_section")
        self.sections.add(name)

    def _require_sections(self) -> None:
        required = (
            "strategy",
            "engine",
            "capital",
            "costs",
            "portfolio",
        )
        for name in required:
            if name not in self.sections:
                raise self._end(f"missing section '{name}'", "missing_section")
        if not self.assets:
            raise self._end("missing section 'asset'", "missing_section")
        if not self.signals:
            raise self._end("missing section 'signal'", "missing_section")
        if not self.actions:
            raise self._end("missing section 'action'", "missing_section")
        if not self.rules:
            raise self._end("missing section 'rule'", "missing_section")

    def _parse_strategy(self, ident: str, header: _Line, body: list[_Line]) -> dict[str, Any]:
        fields = self._fields(body)
        name = self._take(fields, "name", header)
        description = fields.pop("description", None)
        self._reject_unknown(fields)
        result: dict[str, Any] = {"id": ident, "name": self._string(name[0], name[1])}
        if description is not None:
            result["description"] = self._string(description[0], description[1])
        return result

    def _parse_section(self, name: str, header: _Line, body: list[_Line]) -> dict[str, Any]:
        if name == "engine":
            return self._parse_engine(header, body)
        if name == "capital":
            return self._parse_capital(header, body)
        if name == "costs":
            return self._parse_costs(header, body)
        return self._parse_portfolio(header, body)

    def _parse_engine(self, header: _Line, body: list[_Line]) -> dict[str, Any]:
        fields = self._fields(body)
        bar_size = self._take(fields, "bar_size", header)
        event = self._take(fields, "event", header)
        fill = self._take(fields, "fill_price_mode", header)
        warmup = self._take(fields, "warmup_bars", header)
        self._reject_unknown(fields)
        return {
            "bar_size": self._word(bar_size[0], bar_size[1]),
            "event": self._word(event[0], event[1]),
            "fill_price_mode": self._word(fill[0], fill[1]),
            "warmup_bars": self._number(warmup[0], warmup[1]),
        }

    def _parse_capital(self, header: _Line, body: list[_Line]) -> dict[str, Any]:
        fields = self._fields(body)
        initial = self._take(fields, "initial", header)
        self._reject_unknown(fields)
        amount, currency = self._amount(initial[0], initial[1])
        return {"initial": amount, "currency": currency}

    def _parse_costs(self, header: _Line, body: list[_Line]) -> dict[str, Any]:
        fields = self._fields(body)
        commission = self._take(fields, "commission_bps", header)
        slippage = self._take(fields, "slippage_bps", header)
        self._reject_unknown(fields)
        return {
            "commission_bps": self._number(commission[0], commission[1]),
            "slippage_bps": self._number(slippage[0], slippage[1]),
        }

    def _parse_portfolio(self, header: _Line, body: list[_Line]) -> dict[str, Any]:
        fields = self._fields(body)
        mode = self._take(fields, "mode", header)
        direction = self._take(fields, "direction", header)
        chain = self._take(fields, "chain", header)
        positions = self._take(fields, "max_positions", header)
        pyramiding = self._take(fields, "allow_pyramiding", header)
        quote = self._take(fields, "quote_currency", header)
        self._reject_unknown(fields)
        return {
            "mode": self._word(mode[0], mode[1]),
            "direction": self._word(direction[0], direction[1]),
            "chain": self._word(chain[0], chain[1]),
            "max_positions": self._number(positions[0], positions[1]),
            "allow_pyramiding": self._bool(pyramiding[0], pyramiding[1]),
            "quote_currency": self._word(quote[0], quote[1]),
        }

    def _parse_asset(self, ident: str, header: _Line, body: list[_Line]) -> dict[str, Any]:
        self._bind_id(self.asset_ids, ident, header)
        fields = self._fields(body)
        symbol = self._take(fields, "symbol", header)
        chain = self._take(fields, "chain", header)
        market = self._take(fields, "market", header)
        quote = self._take(fields, "quote", header)
        self._reject_unknown(fields)
        return {
            "id": ident,
            "symbol": self._word(symbol[0], symbol[1]),
            "chain": self._word(chain[0], chain[1]),
            "market": self._word(market[0], market[1]),
            "quote": self._word(quote[0], quote[1]),
        }

    def _parse_series(self, matched: re.Match[str], line: _Line) -> dict[str, Any]:
        ident = matched.group(1)
        expr_text = matched.group(2).strip()
        expr_offset = line.offset + matched.start(2)
        if ident in BUILTIN_SERIES:
            raise self._error(
                line,
                f"'{ident}' is a built-in series and cannot be redefined",
                "reserved_series",
            )
        self._bind_expr_name(ident, line)
        try:
            node = parse(expr_text, names=self.names)
        except ExprError as err:
            raise self._shift(err, expr_offset) from None
        self._reject_deep_expr(node, expr_offset)
        if not isinstance(node, IndicatorCall):
            raise self._error(line, "series formula must be an indicator call", "invalid_series")
        if not isinstance(node.source, Ref):
            raise self._error(line, "indicator source must be a series name", "invalid_series")
        params: dict[str, int | float] = {}
        for pname, value in node.params:
            if not isinstance(value, Lit) or isinstance(value.value, bool):
                raise self._error(
                    line,
                    f"indicator parameter '{pname}' must be a numeric literal",
                    "invalid_series",
                )
            params[pname] = value.value
        series: dict[str, Any] = {
            "id": ident,
            "kind": "indicator",
            "indicator": node.name,
            "source": node.source.name,
            "params": params,
        }
        if node.output is not None:
            series["output"] = node.output
        self.names[ident] = ExprType.NUMERIC
        self.bound.add(ident)
        return series

    def _parse_signal(self, matched: re.Match[str], line: _Line) -> dict[str, Any]:
        ident = matched.group(1)
        expr_text = matched.group(2).strip()
        expr_offset = line.offset + matched.start(2)
        self._bind_expr_name(ident, line)
        try:
            node = parse_condition(expr_text, names=self.names)
        except ExprError as err:
            raise self._shift(err, expr_offset) from None
        self._reject_deep_expr(node, expr_offset)
        try:
            expr = to_ir(node)
        except ExprError as err:
            raise self._shift(err, expr_offset) from None
        self.names[ident] = ExprType.BOOLEAN
        self.bound.add(ident)
        return {"id": ident, "expr": expr}

    def _parse_action(self, ident: str, header: _Line, body: list[_Line]) -> dict[str, Any]:
        self._bind_id(self.action_ids, ident, header)
        fields = self._fields(body)
        kind = self._take(fields, "type", header)
        side = self._take(fields, "side", header)
        asset = self._take(fields, "asset", header)
        order = self._take(fields, "order", header)
        sizing = self._take(fields, "sizing", header)
        risk = fields.pop("risk oco", None)
        self._reject_unknown(fields)
        action: dict[str, Any] = {
            "id": ident,
            "type": self._word(kind[0], kind[1]),
            "side": self._word(side[0], side[1]),
            "asset": self._ident(asset[0], asset[1]),
            "order": {"type": self._word(order[0], order[1])},
            "sizing": self._sizing(sizing[0], sizing[1]),
        }
        if risk is not None:
            action["risk"] = self._risk(risk[0], risk[2])
        return action

    def _risk(self, header: _Line, children: list[_Line]) -> dict[str, Any]:
        if header.indent != 2:
            raise self._error(header, "risk block must be indented under the action", "syntax")
        fields = self._fields(children)
        evaluation = self._take(fields, "evaluation", header)
        movement = self._take(fields, "movement", header)
        take_profit = self._take(fields, "take_profit", header)
        stop_loss = self._take(fields, "stop_loss", header)
        self._reject_unknown(fields)
        return {
            "type": "oco",
            "evaluation": self._word(evaluation[0], evaluation[1]),
            "movement": self._word(movement[0], movement[1]),
            "take_profit": {
                "mode": "percent",
                "percent": self._percent(take_profit[0], take_profit[1]),
            },
            "stop_loss": {
                "mode": "percent",
                "percent": self._percent(stop_loss[0], stop_loss[1]),
            },
        }

    def _parse_rule(self, ident: str, header: _Line, body: list[_Line]) -> dict[str, Any]:
        self._bind_id(self.rule_ids, ident, header)
        if len(body) != 1:
            raise self._error(header, "rule body must be one 'when ... do ...' line", "syntax")
        line = body[0]
        matched = _WHEN.fullmatch(line.text)
        if matched is None:
            raise self._error(line, "expected 'when <signal> do <action>'", "syntax")
        return {"id": ident, "when": matched.group(1), "do": matched.group(2)}

    def _fields(self, body: list[_Line]) -> dict[str, tuple[_Line, str, list[_Line]]]:
        grouped = _group(body)
        fields: dict[str, tuple[_Line, str, list[_Line]]] = {}
        for line, children in grouped:
            if ":" not in line.text:
                raise self._error(line, f"expected a field, got '{line.text}'", "syntax")
            key, _, raw = line.text.partition(":")
            key = key.strip()
            if not key:
                raise self._error(line, "expected a field name", "syntax")
            if key in fields:
                raise self._error(line, f"duplicate field '{key}'", "duplicate_field")
            if children and line.indent >= children[0].indent:
                raise self._error(children[0], "unexpected indent", "syntax")
            fields[key] = (line, raw.strip(), children)
        return fields

    def _take(
        self,
        fields: dict[str, tuple[_Line, str, list[_Line]]],
        key: str,
        header: _Line,
    ) -> tuple[_Line, str, list[_Line]]:
        found = fields.pop(key, None)
        if found is None:
            raise self._error(header, f"missing field '{key}'", "missing_field")
        if found[2]:
            raise self._error(found[0], f"field '{key}' does not take a nested block", "syntax")
        return found

    def _reject_unknown(self, fields: dict[str, tuple[_Line, str, list[_Line]]]) -> None:
        for key, (line, _value, _children) in fields.items():
            raise self._error(line, f"unknown field '{key}'", "unknown_field")

    def _bind_expr_name(self, ident: str, line: _Line) -> None:
        if ident in self.bound or ident in BUILTIN_SERIES:
            raise self._error(line, f"duplicate id '{ident}'", "duplicate_id")

    def _bind_id(self, seen: set[str], ident: str, line: _Line) -> None:
        if ident in seen:
            raise self._error(line, f"duplicate id '{ident}'", "duplicate_id")
        seen.add(ident)

    def _string(self, line: _Line, raw: str) -> str:
        if not raw.startswith('"'):
            raise self._error(line, "expected a quoted string", "syntax")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise self._error(line, "invalid string", "syntax") from None
        if not isinstance(value, str):
            raise self._error(line, "expected a string", "syntax")
        return value

    def _number(self, line: _Line, raw: str) -> int | float:
        if not _NUMBER.fullmatch(raw):
            raise self._error(line, f"expected a number, got '{raw}'", "syntax")
        digits = sum(ch.isdigit() for ch in raw)
        if digits > MAX_NUMBER_LITERAL_DIGITS:
            raise self._error(line, "bad number", "bad_number")
        if re.fullmatch(r"\d+", raw):
            return int(raw)
        value = float(raw)
        if value != value or value in {float("inf"), float("-inf")}:
            raise self._error(line, "bad number", "bad_number")
        return value

    def _amount(self, line: _Line, raw: str) -> tuple[int | float, str]:
        parts = raw.split()
        if len(parts) != 2:
            raise self._error(line, "expected '<number> <currency>'", "syntax")
        return self._number(line, parts[0]), self._word(line, parts[1])

    def _word(self, line: _Line, raw: str) -> str:
        if not _TOKEN.fullmatch(raw):
            raise self._error(line, f"expected a word, got '{raw}'", "syntax")
        return raw

    def _ident(self, line: _Line, raw: str) -> str:
        if not re.fullmatch(_IDENT, raw):
            raise self._error(line, f"invalid identifier '{raw}'", "syntax")
        return raw

    def _bool(self, line: _Line, raw: str) -> bool:
        folded = raw.lower()
        if folded not in {"true", "false"}:
            raise self._error(line, f"expected true or false, got '{raw}'", "syntax")
        return folded == "true"

    def _sizing(self, line: _Line, raw: str) -> dict[str, Any]:
        matched = _SIZING.fullmatch(raw)
        if matched is None:
            raise self._error(line, "expected '<number>% equity' or '<number>% position'", "syntax")
        percent = self._number(line, matched.group(1))
        mode = "percent_equity" if matched.group(2) == "equity" else "percent_position"
        return {"mode": mode, "percent": percent}

    def _percent(self, line: _Line, raw: str) -> int | float:
        matched = _PERCENT.fullmatch(raw)
        if matched is None:
            raise self._error(line, "expected '<number>%'", "syntax")
        return self._number(line, matched.group(1))

    def _reject_deep_expr(self, node: Expr, expr_offset: int) -> None:
        """Reject a formula whose IR tree is deeper than `from_ir` allows.

        The expression parser counts parentheses, calls, `NOT`, and unary
        minus. `from_ir`, `validate_ir`, and `canonicalize` count every node,
        so a flat chain can pass `parse` and then blow the stack. Risk fields
        are numbers and words, not formulas, so they have no tree to check.
        The error points at the start of the formula.
        """
        if not _deeper_than_ir_limit(node):
            return
        line, column = _position(self.source, expr_offset)
        raise StrategyError(
            f"expression nested too deeply (limit {MAX_EXPR_DEPTH})",
            code="expr_too_deep",
            offset=expr_offset,
            line=line,
            column=column,
        )

    def _shift(self, err: ExprError, start: int) -> StrategyError:
        offset = start + err.offset
        line, column = _position(self.source, offset)
        return StrategyError(
            err.message,
            code=err.code or "syntax",
            offset=offset,
            line=line,
            column=column,
            path=err.path,
        )

    def _error(self, line: _Line, message: str, code: str) -> StrategyError:
        return StrategyError(
            message,
            code=code,
            offset=line.offset,
            line=line.line,
            column=line.column,
        )

    def _end(self, message: str, code: str) -> StrategyError:
        offset = len(self.source)
        line, column = _position(self.source, offset)
        return StrategyError(message, code=code, offset=offset, line=line, column=column)


def parse_strategy(source: str) -> dict[str, Any]:
    """Parse DSL text into canonical IR v1.

    Raises `StrategyError` for syntax errors, expression errors, duplicate
    expression ids, and reserved series names. Other semantic failures are
    left in the IR for `validate_ir`. A lone surrogate is rejected here,
    before canonicalization would try to encode it.
    """
    if not isinstance(source, str):
        raise TypeError("strategy must be a string")
    _reject_lone_surrogate(source)
    lines = _collect_lines(source)
    document = _StrategyParser(source, lines).parse()
    return canonicalize(document)


def _collect_lines(source: str) -> list[_Line]:
    lines: list[_Line] = []
    for line_no, start, raw in _iter_physical(source):
        # rstrip drops a trailing space, tab, or NBSP, including the space
        # before `#`, so `engine: # note` and `risk oco: ` stay headers.
        stripped = _strip_comment(raw).rstrip()
        if stripped.strip() == "":
            continue
        indent_text = stripped[: len(stripped) - len(stripped.lstrip(" \t"))]
        if "\t" in indent_text:
            raise StrategyError(
                "indentation must use spaces",
                code="syntax",
                offset=start,
                line=line_no,
                column=1,
            )
        indent = len(indent_text)
        if indent % 2 != 0:
            raise StrategyError(
                "indentation must be a multiple of 2 spaces",
                code="syntax",
                offset=start + indent,
                line=line_no,
                column=indent + 1,
            )
        text = stripped[indent:]
        lines.append(_Line(indent, text, start + indent, line_no, indent + 1))
    return lines


def _iter_physical(source: str) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    index = 0
    line_no = 1
    length = len(source)
    while index < length:
        start = index
        while index < length and source[index] not in "\r\n":
            index += 1
        rows.append((line_no, start, source[start:index]))
        if index < length and source[index] == "\r":
            index += 1
            if index < length and source[index] == "\n":
                index += 1
        elif index < length and source[index] == "\n":
            index += 1
        line_no += 1
    return rows


def _strip_comment(text: str) -> str:
    quote = False
    escape = False
    for index, ch in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                quote = False
            continue
        if ch == '"':
            quote = True
        elif ch == "#":
            return text[:index]
    return text


def _deeper_than_ir_limit(node: Expr) -> bool:
    """True when `from_ir` would raise `expr_too_deep` for this tree.

    `from_ir` visits the root at depth 0 and each child at depth + 1, and
    fails when depth is greater than `MAX_EXPR_DEPTH`. The walk is a stack,
    so a long left-associated chain does not recurse.
    """
    stack: list[tuple[Expr, int]] = [(node, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_EXPR_DEPTH:
            return True
        if isinstance(current, Unary):
            stack.append((current.arg, depth + 1))
        elif isinstance(current, Binary):
            stack.append((current.left, depth + 1))
            stack.append((current.right, depth + 1))
        elif isinstance(current, Call):
            stack.extend((arg, depth + 1) for arg in current.args)
        elif isinstance(current, IndicatorCall):
            stack.append((current.source, depth + 1))
            stack.extend((value, depth + 1) for _name, value in current.params)
    return False


def _group(body: list[_Line]) -> list[tuple[_Line, list[_Line]]]:
    grouped: list[tuple[_Line, list[_Line]]] = []
    index = 0
    while index < len(body):
        line = body[index]
        children: list[_Line] = []
        index += 1
        if line.text.endswith(":"):
            while index < len(body) and body[index].indent > line.indent:
                children.append(body[index])
                index += 1
        grouped.append((line, children))
    return grouped


def _reject_lone_surrogate(source: str) -> None:
    index = lone_surrogate_at(source)
    if index is None:
        return
    line, column = _position(source, index)
    raise StrategyError(
        "lone surrogate is not valid text",
        code="invalid_string",
        offset=index,
        line=line,
        column=column,
    )


def _position(source: str, offset: int) -> tuple[int, int]:
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
