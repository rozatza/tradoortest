"""CSV and JSON trade logs, and the same pair for the ratio report.

Both encodings of a trade use the same strings. A bool is ``true`` or
``false``. A null (an open trade's exit, or an undefined ratio) is JSON
``null`` and an empty CSV cell. Money is USDC cents with two decimal places.
A ratio uses ``format(value, "f")`` so a zero is not ``0E-10``. See
``docs/metrics.md``.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal

from app.backtest.metrics import Metrics, Trade, _local_ratio

SCHEMA = "tradoortest.trade_log.v1"
METRICS_SCHEMA = "tradoortest.metrics.v1"

METRIC_COLUMNS: tuple[str, ...] = (
    "net_return",
    "annualized_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "payoff_ratio",
)

TRADE_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "asset_id",
    "status",
    "entry_time",
    "exit_time",
    "qty",
    "entry_price",
    "exit_price",
    "entry_notional",
    "exit_notional",
    "commission",
    "realized_pnl",
    "unrealized_pnl",
    "limit_below_low",
    "overlaps_suspect_outage",
    "synthetic_bars",
    "missing_bars",
    "long_no_trade_bars",
    "long_no_trade_minutes",
    "suspect_outage_bars",
    "suspect_outage_minutes",
)

_MONEY = {
    "entry_notional",
    "exit_notional",
    "commission",
    "realized_pnl",
    "unrealized_pnl",
}
_PRICE = {"entry_price", "exit_price", "qty"}
_BOOL = {"limit_below_low", "overlaps_suspect_outage"}


def trade_rows(trades: Sequence[Trade]) -> list[dict[str, str | None]]:
    """One dict per trade, keys in ``TRADE_COLUMNS`` order.

    A null is ``None`` here and in the JSON. The CSV writes that same null
    as an empty cell.
    """
    return [{column: _cell(trade, column) for column in TRADE_COLUMNS} for trade in trades]


def trade_log_csv(trades: Sequence[Trade]) -> str:
    """CSV of ``trades``. The header is ``TRADE_COLUMNS``. A null is an empty cell."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TRADE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in trade_rows(trades):
        writer.writerow({key: "" if value is None else value for key, value in row.items()})
    return buffer.getvalue()


def trade_log_json(trades: Sequence[Trade]) -> str:
    """JSON document. Null cells are JSON ``null``, and the other cells match the CSV."""
    payload = {"schema": SCHEMA, "trades": trade_rows(trades)}
    return json.dumps(payload, indent=2) + "\n"


def metrics_csv(metrics: Metrics) -> str:
    """One CSV row of the ratios. An undefined ratio is an empty cell."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=METRIC_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerow({column: _ratio_cell(metrics, column) or "" for column in METRIC_COLUMNS})
    return buffer.getvalue()


def metrics_json(metrics: Metrics) -> str:
    """JSON ratios. An undefined ratio is ``null``, and ``undefined`` maps the reason.

    Defined ratios are ``format(value, "f")``, the same characters as the CSV.
    """
    values = {column: _ratio_cell(metrics, column) for column in METRIC_COLUMNS}
    payload = {
        "schema": METRICS_SCHEMA,
        "values": values,
        "undefined": {
            key: metrics.undefined[key] for key in METRIC_COLUMNS if key in metrics.undefined
        },
    }
    return json.dumps(payload, indent=2) + "\n"


def _ratio_cell(metrics: Metrics, column: str) -> str | None:
    value = getattr(metrics, column)
    if value is None:
        return None
    return format(value, "f")


def _cell(trade: Trade, column: str) -> str | None:
    value = getattr(trade, column)
    if value is None:
        return None
    if column in _BOOL:
        return "true" if value else "false"
    if column in _MONEY:
        return format(_cents(value), "f")
    if column in _PRICE:
        return format(value, "f")
    return str(value)


def _cents(amount: Decimal) -> Decimal:
    """Two decimal places in the fresh 50-digit context.

    A far mark is already a cent. Quantizing it in the caller's precision
    raises ``InvalidOperation`` once the number needs more than that precision.
    """
    with _local_ratio():
        return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
