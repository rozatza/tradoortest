"""Run one stored backtest request and return a JSON-ready result.

The result uses null for an undefined ratio and a string for every decimal.
It does not contain NaN or infinity.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.backtest import run_backtest
from app.backtest.engine import BacktestResult
from app.backtest.log import METRIC_COLUMNS, trade_rows
from app.ohlcv.bars import Bar

_RESULT_SCHEMA = "tradoortest.backtest_result.v1"
_MINT = "backtest"


def run_backtest_job(body: Mapping[str, Any]) -> dict[str, Any]:
    """Execute ``body`` and return the persisted result document."""
    document = body["ir"]
    if not isinstance(document, Mapping):
        raise TypeError("job ir must be an object")
    bar_size = document["engine"]["bar_size"]
    bars = {
        asset_id: _bars(asset_id, rows, bar_size)
        for asset_id, rows in body["bars"].items()
    }
    minutes = body.get("long_no_trade_minutes", 120)
    result = run_backtest(document, bars, long_no_trade_minutes=int(minutes))
    payload = _payload(result)
    json.dumps(payload, allow_nan=False)
    return payload


def _bars(asset_id: str, rows: object, bar_size: str) -> list[Bar]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise TypeError(f"bars for {asset_id} must be a list")
    built: list[Bar] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError(f"bar for {asset_id} must be an object")
        synthetic = bool(row.get("synthetic", False))
        close = float(row["close"])
        if synthetic:
            built.append(
                Bar(
                    _MINT,
                    bar_size,
                    int(row["open_time"]),
                    close,
                    close,
                    close,
                    close,
                    0.0,
                    True,
                )
            )
            continue
        built.append(
            Bar(
                _MINT,
                bar_size,
                int(row["open_time"]),
                float(row.get("open", close)),
                float(row.get("high", close)),
                float(row.get("low", close)),
                close,
                float(row.get("volume", 1)),
                False,
            )
        )
    return built


def _payload(result: BacktestResult) -> dict[str, Any]:
    metrics = result.metrics
    values: dict[str, str | None] = {}
    for column in METRIC_COLUMNS:
        value = getattr(metrics, column)
        values[column] = None if value is None else _decimal(value)
    undefined = {
        key: metrics.undefined[key] for key in METRIC_COLUMNS if key in metrics.undefined
    }
    payload = {
        "schema": _RESULT_SCHEMA,
        "final_equity": _decimal(result.final_equity),
        "final_cash": _decimal(result.final_cash),
        "lints": list(result.lints),
        "metrics": {
            "values": values,
            "undefined": undefined,
            "initial_equity": _decimal(metrics.initial_equity),
            "final_equity": _decimal(metrics.final_equity),
            "bar_count": metrics.bar_count,
            "bars_per_year": metrics.bars_per_year,
            "grid_bars": metrics.grid_bars,
            "missing_bars": metrics.missing_bars,
            "synthetic_bars": metrics.synthetic_bars,
            "long_no_trade_bars": metrics.long_no_trade_bars,
            "long_no_trade_minutes": metrics.long_no_trade_minutes,
            "long_no_trade_threshold": metrics.long_no_trade_threshold,
            "suspect_outage": metrics.suspect_outage,
            "suspect_outage_bars": metrics.suspect_outage_bars,
            "suspect_outage_minutes": metrics.suspect_outage_minutes,
            "missing_bars_in_position": metrics.missing_bars_in_position,
            "synthetic_bars_in_position": metrics.synthetic_bars_in_position,
            "long_no_trade_bars_in_position": metrics.long_no_trade_bars_in_position,
            "long_no_trade_minutes_in_position": metrics.long_no_trade_minutes_in_position,
            "suspect_outage_bars_in_position": metrics.suspect_outage_bars_in_position,
            "suspect_outage_minutes_in_position": metrics.suspect_outage_minutes_in_position,
            "take_profit_limit_below_low": metrics.take_profit_limit_below_low,
        },
        "trades": trade_rows(result.trades),
        "fills": [
            {
                "open_time": fill.open_time,
                "asset_id": fill.asset_id,
                "side": fill.side,
                "leg": fill.leg,
                "qty": _decimal(fill.qty),
                "price": _decimal(fill.price),
                "notional": _decimal(fill.notional),
                "commission": _decimal(fill.commission),
                "equity_after": _decimal(fill.equity_after),
                "limit_below_low": fill.limit_below_low,
            }
            for fill in result.fills
        ],
    }
    _reject_non_finite(payload)
    return payload


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("result decimal is not finite")
    return format(value, "f")


def _reject_non_finite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("result JSON would contain a non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)
