"""Trade-log and metrics files from a stored backtest result.

The cells are the strings already on the result. Nothing here recomputes a
ratio, a fill, or an equity mark. JSON exports match ``trade_log_json`` and
``metrics_json`` and are not formula-escaped. Every CSV cell, trades and
metrics, goes through :func:`csv_cell`. A cell is copied raw only when it
matches the strict decimal grammar, or when it is a declared integer or ISO
timestamp column whose text matches that format. Any other cell that starts
with ``=``, ``+``, ``-``, ``@``, a tab, or a carriage return is written with
a leading apostrophe. A new column has no raw exception.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from collections.abc import Mapping
from typing import Any

from app.backtest.log import METRIC_COLUMNS, METRICS_SCHEMA, SCHEMA, TRADE_COLUMNS

EXPORT_NAMES = (
    "trades.csv",
    "trades.json",
    "metrics.csv",
    "metrics.json",
)


class ExportError(ValueError):
    """The stored result is missing a trade-log or metrics cell."""


# Same text as ``DECIMAL_MARK`` in web/lib/backtest.ts and ``format(Decimal, "f")``:
# optional minus, no exponent, no leading zeros, optional fraction.
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")
_ISO_TIMESTAMP = re.compile(
    r"[+-]?[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z"
)

# Unix seconds and counts. A match is still required; the name alone is not raw.
INTEGER_COLUMNS = frozenset(
    {
        "entry_time",
        "exit_time",
        "synthetic_bars",
        "missing_bars",
        "long_no_trade_bars",
        "long_no_trade_minutes",
        "suspect_outage_bars",
        "suspect_outage_minutes",
    }
)

# No current export column is an ISO timestamp. A later column is raw only
# when its text matches ``_ISO_TIMESTAMP``.
TIMESTAMP_COLUMNS: frozenset[str] = frozenset()

_FORMULA_LEAD = frozenset("=+-@\t\r")
# Fullwidth ＝ ＋ － ＠. A spreadsheet can treat these as formula starts.
_FORMULA_LOOKALIKE = frozenset("\uff1d\uff0b\uff0d\uff20")
# Space, tab, CR, LF, NBSP, and a BOM may sit in front of a formula start.
_FORMULA_PAD = frozenset(" \t\r\n\u00a0\ufeff")


def _strict_decimal(value: str) -> bool:
    """True only for the decimal grammar. Always-true would publish formulas."""
    return _DECIMAL.fullmatch(value) is not None


def csv_cell(column: str, value: str | None) -> str:
    """Write one CSV cell. This is the only CSV cell writer.

    Raw when ``value`` is a strict decimal, or when ``column`` is a declared
    integer or ISO timestamp column and ``value`` matches that format.
    Otherwise a formula start is prefixed with an apostrophe. That includes
    ``= + - @`` tab or CR, a fullwidth lookalike, and the same characters
    after leading space, NBSP, or a BOM. ``None`` is an empty cell. An
    unknown column cannot skip the apostrophe.
    """
    if value is None:
        return ""
    if _strict_decimal(value):
        return value
    if column in INTEGER_COLUMNS and _INTEGER.fullmatch(value):
        return value
    if column in TIMESTAMP_COLUMNS and _ISO_TIMESTAMP.fullmatch(value):
        return value
    if _formula_prefixed(value):
        return "'" + value
    return value


def _formula_prefixed(value: str) -> bool:
    """True when the cell would open as a formula, including a padded start."""
    if value[:1] in _FORMULA_LEAD or value[:1] in _FORMULA_LOOKALIKE:
        return True
    index = 0
    while index < len(value) and value[index] in _FORMULA_PAD:
        index += 1
    if index == 0 or index >= len(value):
        return False
    character = value[index]
    return character in _FORMULA_LEAD or character in _FORMULA_LOOKALIKE


def render_export(name: str, result: Mapping[str, Any]) -> str:
    """Return the file body for one allowlisted export name.

    Call this off the event loop. A running loop on this thread means the
    request handler rendered the file inline.
    """
    _refuse_event_loop()
    if name == "trades.csv":
        return _trades_csv(result)
    if name == "trades.json":
        return _trades_json(result)
    if name == "metrics.csv":
        return _metrics_csv(result)
    if name == "metrics.json":
        return _metrics_json(result)
    raise ExportError(name)


def _refuse_event_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("export render is on the event loop")


def _trades_csv(result: Mapping[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TRADE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _trade_rows(result):
        writer.writerow({key: csv_cell(key, value) for key, value in row.items()})
    return buffer.getvalue()


def _trades_json(result: Mapping[str, Any]) -> str:
    payload = {"schema": SCHEMA, "trades": _trade_rows(result)}
    return json.dumps(payload, indent=2) + "\n"


def _metrics_csv(result: Mapping[str, Any]) -> str:
    """The same :func:`csv_cell` path as the trade log."""
    values = _metric_values(result)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=METRIC_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerow({column: csv_cell(column, values[column]) for column in METRIC_COLUMNS})
    return buffer.getvalue()


def _metrics_json(result: Mapping[str, Any]) -> str:
    payload = {
        "schema": METRICS_SCHEMA,
        "values": _metric_values(result),
        "undefined": _undefined(result),
    }
    return json.dumps(payload, indent=2) + "\n"


def _trade_rows(result: Mapping[str, Any]) -> list[dict[str, str | None]]:
    trades = result.get("trades")
    if not isinstance(trades, list):
        raise ExportError("trades")
    rows: list[dict[str, str | None]] = []
    for trade in trades:
        if not isinstance(trade, dict):
            raise ExportError("trades")
        row: dict[str, str | None] = {}
        for column in TRADE_COLUMNS:
            if column not in trade:
                raise ExportError(column)
            value = trade[column]
            if value is None:
                row[column] = None
            elif isinstance(value, str):
                row[column] = value
            else:
                raise ExportError(column)
        rows.append(row)
    return rows


def _metrics(result: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        raise ExportError("metrics")
    return metrics


def _metric_values(result: Mapping[str, Any]) -> dict[str, str | None]:
    values = _metrics(result).get("values")
    if not isinstance(values, dict):
        raise ExportError("values")
    copied: dict[str, str | None] = {}
    for column in METRIC_COLUMNS:
        if column not in values:
            raise ExportError(column)
        value = values[column]
        if value is None:
            copied[column] = None
        elif isinstance(value, str):
            copied[column] = value
        else:
            raise ExportError(column)
    return copied


def _undefined(result: Mapping[str, Any]) -> dict[str, str]:
    raw = _metrics(result).get("undefined")
    if not isinstance(raw, dict):
        raise ExportError("undefined")
    kept: dict[str, str] = {}
    for column in METRIC_COLUMNS:
        if column not in raw:
            continue
        reason = raw[column]
        if not isinstance(reason, str):
            raise ExportError(column)
        kept[column] = reason
    return kept
