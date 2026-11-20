"""Trade-log and metrics files from a stored backtest result.

The cells are the strings already on the result. Nothing here recomputes a
ratio, a fill, or an equity mark. The bytes match ``trade_log_csv``,
``trade_log_json``, ``metrics_csv``, and ``metrics_json`` for the same run.
"""

from __future__ import annotations

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


def render_export(name: str, result: Mapping[str, Any]) -> str:
    """Return the file body for one allowlisted export name."""
    if name == "trades.csv":
        return _trades_csv(result)
    if name == "trades.json":
        return _trades_json(result)
    if name == "metrics.csv":
        return _metrics_csv(result)
    if name == "metrics.json":
        return _metrics_json(result)
    raise ExportError(name)


def _trades_csv(result: Mapping[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TRADE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in _trade_rows(result):
        writer.writerow({key: "" if value is None else value for key, value in row.items()})
    return buffer.getvalue()


def _trades_json(result: Mapping[str, Any]) -> str:
    payload = {"schema": SCHEMA, "trades": _trade_rows(result)}
    return json.dumps(payload, indent=2) + "\n"


def _metrics_csv(result: Mapping[str, Any]) -> str:
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
