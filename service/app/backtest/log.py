"""CSV and JSON trade logs from the same trades.

Both encodings use the same strings. A bool is ``true`` or ``false``. An
empty exit field is an empty string. Money is USDC cents with two decimal
places. See ``docs/metrics.md`` for the columns.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from decimal import Decimal

from app.backtest.metrics import Trade

SCHEMA = "tradoortest.trade_log.v1"

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
_OPTIONAL = {"exit_time", "exit_price"}


def trade_rows(trades: Sequence[Trade]) -> list[dict[str, str]]:
    """One dict per trade, keys in ``TRADE_COLUMNS`` order, values already encoded."""
    return [{column: _cell(trade, column) for column in TRADE_COLUMNS} for trade in trades]


def trade_log_csv(trades: Sequence[Trade]) -> str:
    """CSV of ``trades``. The header is ``TRADE_COLUMNS`` and the cells match the JSON."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TRADE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in trade_rows(trades):
        writer.writerow(row)
    return buffer.getvalue()


def trade_log_json(trades: Sequence[Trade]) -> str:
    """JSON document whose ``trades`` objects use the same strings as the CSV cells."""
    payload = {"schema": SCHEMA, "trades": trade_rows(trades)}
    return json.dumps(payload, indent=2) + "\n"


def _cell(trade: Trade, column: str) -> str:
    value = getattr(trade, column)
    if column in _OPTIONAL and value is None:
        return ""
    if column in _BOOL:
        return "true" if value else "false"
    if column in _MONEY:
        return format(_cents(value), "f")
    if column in _PRICE:
        return format(value, "f")
    return str(value)


def _cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"))
