"""Render canonical IR v1 as strategy DSL text.

The text is the form `parse_strategy` reads back. Sections are separated by
one blank line. A second format of the same IR is identical.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.expr import ExprType, format_expr, from_ir
from app.ir.canonical import canonicalize, format_number
from app.ir.constants import BUILTIN_SERIES, INDICATOR_PARAMS

_INDICATOR_DSL = {
    "sma": "SMA",
    "ema": "EMA",
    "atr": "ATR",
    "rsi": "RSI",
    "macd": "MACD",
    "bollinger": "BOLLINGER",
    "stochastic": "STOCHASTIC",
}


def format_strategy(document: Mapping[str, Any]) -> str:
    """Return the canonical DSL for an IR document. The result ends with a newline."""
    ir = canonicalize(document)
    sections = [
        _strategy(ir["strategy"]),
        _engine(ir["engine"]),
        _capital(ir["capital"]),
        _costs(ir["costs"]),
        "\n".join(_asset(asset) for asset in ir["assets"]),
    ]
    if ir["derived_series"]:
        sections.append("\n".join(_series(series) for series in ir["derived_series"]))
    names = _expr_names(ir)
    sections.append("\n".join(_signal(signal, names) for signal in ir["signals"]))
    sections.append("\n".join(_action(action) for action in ir["actions"]))
    sections.append("\n".join(_rule(rule) for rule in ir["rules"]))
    sections.append(_portfolio(ir["portfolio"]))
    return "\n\n".join(sections) + "\n"


def _expr_names(ir: Mapping[str, Any]) -> dict[str, ExprType]:
    names = {name: ExprType.NUMERIC for name in BUILTIN_SERIES}
    for series in ir["derived_series"]:
        names[series["id"]] = ExprType.NUMERIC
    for signal in ir["signals"]:
        names[signal["id"]] = ExprType.BOOLEAN
    return names


def _strategy(strategy: Mapping[str, Any]) -> str:
    name = json.dumps(strategy["name"], ensure_ascii=False)
    lines = [f"strategy {strategy['id']}:", f"  name: {name}"]
    if "description" in strategy:
        text = json.dumps(strategy["description"], ensure_ascii=False)
        lines.append(f"  description: {text}")
    return "\n".join(lines)


def _engine(engine: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "engine:",
            f"  bar_size: {engine['bar_size']}",
            f"  event: {engine['event']}",
            f"  fill_price_mode: {engine['fill_price_mode']}",
            f"  warmup_bars: {format_number(engine['warmup_bars'])}",
        ]
    )


def _capital(capital: Mapping[str, Any]) -> str:
    amount = format_number(capital["initial"])
    return "\n".join(["capital:", f"  initial: {amount} {capital['currency']}"])


def _costs(costs: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "costs:",
            f"  commission_bps: {format_number(costs['commission_bps'])}",
            f"  slippage_bps: {format_number(costs['slippage_bps'])}",
        ]
    )


def _asset(asset: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            f"asset {asset['id']}:",
            f"  symbol: {asset['symbol']}",
            f"  chain: {asset['chain']}",
            f"  market: {asset['market']}",
            f"  quote: {asset['quote']}",
        ]
    )


def _series(series: Mapping[str, Any]) -> str:
    indicator = series["indicator"]
    params = series["params"]
    order = INDICATOR_PARAMS.get(indicator, ())
    parts = [str(series["source"])]
    emitted: set[str] = set()
    for key in order:
        if key in params:
            parts.append(f"{key}={format_number(params[key])}")
            emitted.add(key)
    for key in sorted(params):
        if key not in emitted:
            parts.append(f"{key}={format_number(params[key])}")
    call = f"{_INDICATOR_DSL.get(indicator, indicator)}({', '.join(parts)})"
    if "output" in series:
        call = f"{call}.{series['output']}"
    return f"series {series['id']} = {call}"


def _signal(signal: Mapping[str, Any], names: Mapping[str, ExprType]) -> str:
    text = format_expr(from_ir(signal["expr"], names=names))
    return f"signal {signal['id']} = {text}"


def _action(action: Mapping[str, Any]) -> str:
    sizing = action["sizing"]
    unit = "equity" if sizing["mode"] == "percent_equity" else "position"
    lines = [
        f"action {action['id']}:",
        f"  type: {action['type']}",
        f"  side: {action['side']}",
        f"  asset: {action['asset']}",
        f"  order: {action['order']['type']}",
        f"  sizing: {format_number(sizing['percent'])}% {unit}",
    ]
    if "risk" in action:
        risk = action["risk"]
        lines.append(f"  risk {risk['type']}:")
        lines.append(f"    evaluation: {risk['evaluation']}")
        lines.append(f"    movement: {risk['movement']}")
        lines.append(f"    take_profit: {format_number(risk['take_profit']['percent'])}%")
        lines.append(f"    stop_loss: {format_number(risk['stop_loss']['percent'])}%")
    return "\n".join(lines)


def _rule(rule: Mapping[str, Any]) -> str:
    return f"rule {rule['id']}:\n  when {rule['when']} do {rule['do']}"


def _portfolio(portfolio: Mapping[str, Any]) -> str:
    flag = "true" if portfolio["allow_pyramiding"] else "false"
    return "\n".join(
        [
            "portfolio:",
            f"  mode: {portfolio['mode']}",
            f"  direction: {portfolio['direction']}",
            f"  chain: {portfolio['chain']}",
            f"  max_positions: {format_number(portfolio['max_positions'])}",
            f"  allow_pyramiding: {flag}",
            f"  quote_currency: {portfolio['quote_currency']}",
        ]
    )
