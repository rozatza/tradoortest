"""Trade-log and metrics files from a stored backtest result.

The cells are the strings already on the result. Nothing here recomputes a
ratio, a fill, or an equity mark. JSON exports match ``trade_log_json`` and
``metrics_json``. CSV exports match ``trade_log_csv`` and ``metrics_csv``
except for a text-typed cell whose first character is ``=``, ``+``, ``-``,
``@``, a tab, or a carriage return. That cell is written with a leading
apostrophe so a spreadsheet does not treat it as a formula. Numeric columns
are written exactly, including a leading minus on a decimal such as
``-12.34``. The column kind comes from the trade-log and metrics schemas,
not from the first character of the cell.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
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


# Text and boolean words. A number, a timestamp, and a count are not here.
TEXT_TRADE_COLUMNS = frozenset(
    {
        "trade_id",
        "asset_id",
        "status",
        "limit_below_low",
        "overlaps_suspect_outage",
    }
)

_FORMULA_LEAD = frozenset("=+-@\t\r")


def csv_text_cell(value: str) -> str:
    """Neutralise a text cell that a spreadsheet would treat as a formula."""
    if value[:1] in _FORMULA_LEAD:
        return "'" + value
    return value


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
        writer.writerow({key: _csv_trade_cell(key, value) for key, value in row.items()})
    return buffer.getvalue()


def _csv_trade_cell(column: str, value: str | None) -> str:
    """Write one trade cell. Numeric columns stay exact. Text columns are neutralised."""
    if value is None:
        return ""
    if column in TEXT_TRADE_COLUMNS:
        return csv_text_cell(value)
    return value


def _trades_json(result: Mapping[str, Any]) -> str:
    payload = {"schema": SCHEMA, "trades": _trade_rows(result)}
    return json.dumps(payload, indent=2) + "\n"


def _metrics_csv(result: Mapping[str, Any]) -> str:
    """Ratio columns are numeric. A leading minus is part of the stored decimal."""
    values = _metric_values(result)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=METRIC_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerow(
        {
            column: "" if values[column] is None else values[column]
            for column in METRIC_COLUMNS
        }
    )
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
