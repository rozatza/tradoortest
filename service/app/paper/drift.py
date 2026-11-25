"""Compare paper fills with ``run_backtest`` on the same window.

The window is the stored closed bars from the oldest retained bar through
the paper cursor, the span the broker replays for warmup. Only a fill on
a bar that closed after this registration's ``registered_at`` is compared.
The document is the one pinned on the registration, so ``commission_bps``
and ``slippage_bps`` are that document's costs. ``run_backtest`` is called
and not modified. Bars before this registration warm that call and do not
open a position in it.

The report is built on read. It is not inserted, and the bars are not
written back. There is no cache. Rows ``_purge`` has already deleted are
not reconstructed from the wallet or the position. A ``BAR_MISSED`` from
this registration, a fill log that no longer replays onto that book, a
carried book from an earlier deployment, a warmup longer than the retained
bars, a ``registered_at`` at or before the retention cutoff, or a cursor
that has not reached a closed bar is ``insufficient_data``. Those cases
are not flagged as drift and are not reported as a match.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from fractions import Fraction
from typing import Any

from app.backtest import run_backtest
from app.backtest.errors import BacktestError
from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar
from app.ohlcv.registry import BY_SYMBOL
from app.ohlcv.store import SqliteBarStore

logger = logging.getLogger("app.paper.drift")

PAPER_DRIFT_THRESHOLD_NAME = "PAPER_DRIFT_THRESHOLD"
DEFAULT_PAPER_DRIFT_THRESHOLD = Decimal("0.01")
_THRESHOLD = re.compile(r"^(?:0|[1-9]\d{0,6})(?:\.\d{1,8})?$")
_MAX_THRESHOLD = Decimal("1000000")
_DISPLAY_STEP = Decimal("0.00000001")
_MIN_PLACES = 2


def paper_drift_threshold(raw: str | None = None) -> Decimal:
    """Absolute quote delta that flags a report. Blank keeps ``0.01``.

    ``ValueError`` means the setting is unusable. A difference greater than
    this value flags the report. An equal difference does not.
    """
    if raw is None:
        raw = os.environ.get(PAPER_DRIFT_THRESHOLD_NAME, "")
    text = raw.strip()
    if text == "":
        return DEFAULT_PAPER_DRIFT_THRESHOLD
    if not _THRESHOLD.fullmatch(text):
        raise ValueError
    value = Decimal(text)
    if value > _MAX_THRESHOLD:
        raise ValueError
    return value


def format_amount(value: Decimal | Fraction) -> str:
    """Decimal text, half-even to 8 places, with at least 2 places.

    ``-250749/50`` is ``-5014.98``. Trailing zeros after the second place
    are removed.
    """
    with localcontext(_display_context()):
        if isinstance(value, Fraction):
            number = Decimal(value.numerator) / Decimal(value.denominator)
        else:
            number = Decimal(value)
        quantized = number.quantize(_DISPLAY_STEP, rounding=ROUND_HALF_EVEN)
        if quantized == 0:
            quantized = Decimal(0)
    text = format(quantized, "f")
    sign = ""
    if text.startswith("-"):
        sign = "-"
        text = text[1:]
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    if len(frac) < _MIN_PLACES:
        frac = frac.ljust(_MIN_PLACES, "0")
    return f"{sign}{whole}.{frac}"


def drift_reports(
    store: SqliteBarStore, user_id: str, threshold: Decimal
) -> list[dict[str, Any]]:
    """One report per running registration. Nothing is written."""
    rows = store.list_live_registrations(user_id=user_id)
    cutoff = store.retention_cutoff()
    return [_one(store, user_id, row, cutoff, threshold) for row in rows]


@dataclass(frozen=True, slots=True)
class _Fill:
    open_time: int
    asset_id: str
    side: str
    price: Decimal


def _one(
    store: SqliteBarStore,
    user_id: str,
    row: tuple[str, int, str, str, int],
    cutoff: int,
    threshold: Decimal,
) -> dict[str, Any]:
    strategy_id, version, document_json, _stored_bar_size, registered_at = row
    document = _document(document_json)
    costs = None if document is None else _costs(document)
    if document is None or costs is None:
        return _insufficient(strategy_id, version, "pinned_document", threshold, costs)
    assets = _assets(document)
    bar_size = _bar_size(document)
    if assets is None or bar_size is None:
        return _insufficient(strategy_id, version, "pinned_document", threshold, costs)
    if registered_at <= cutoff:
        return _insufficient(strategy_id, version, "window_before_retained", threshold, costs)
    warmup = _warmup_bars(document)
    if warmup is None:
        return _insufficient(strategy_id, version, "pinned_document", threshold, costs)
    if store.paper_has_kind(
        user_id=user_id,
        strategy_id=strategy_id,
        version=version,
        kind="BAR_MISSED",
        min_open_time=registered_at,
    ):
        return _insufficient(strategy_id, version, "bar_missed", threshold, costs)
    wallet = store.paper_wallet(user_id=user_id, strategy_id=strategy_id, version=version)
    positions = store.paper_positions(user_id=user_id, strategy_id=strategy_id, version=version)
    fills = store.paper_fills(user_id=user_id, strategy_id=strategy_id, version=version)
    if not _fills_cover_book(wallet, positions, fills):
        return _insufficient(strategy_id, version, "purged_fills", threshold, costs)
    step = TIMEFRAME_SECONDS[bar_size]
    if _prior_book(fills, positions, registered_at, step):
        return _insufficient(strategy_id, version, "prior_book", threshold, costs)
    try:
        series, gap, first_index = _window(
            store,
            user_id,
            assets,
            bar_size,
            registered_at,
            strategy_id,
            version,
            warmup,
        )
    except (OSError, ValueError, TypeError):
        return _insufficient(strategy_id, version, "window_before_retained", threshold, costs)
    if gap is not None:
        return _insufficient(strategy_id, version, gap, threshold, costs)
    if series is None:
        return _insufficient(strategy_id, version, "no_window", threshold, costs)
    try:
        result = run_backtest(_gated(document, first_index), series)
    except (BacktestError, DecimalException, ValueError, KeyError, TypeError, ArithmeticError):
        logger.warning(
            "paper drift backtest failed strategy_id=%s version=%s",
            strategy_id,
            version,
        )
        return _insufficient(strategy_id, version, "backtest_failed", threshold, costs)
    paper = [
        _paper_fill(item)
        for item in fills
        if _closed_after(int(item[0]), step, registered_at)
    ]
    engine = [
        _Fill(fill.open_time, fill.asset_id, fill.side, fill.price)
        for fill in result.fills
        if _closed_after(fill.open_time, step, registered_at)
    ]
    mismatches, price_delta = _pair(paper, engine)
    return _compared(
        strategy_id,
        version,
        threshold,
        costs,
        paper_count=len(paper),
        backtest_count=len(engine),
        mismatches=mismatches,
        price_delta=price_delta,
        paper_realized=_paper_realized(positions),
        backtest_realized=_backtest_realized(result.trades),
    )


def _compared(
    strategy_id: str,
    version: int,
    threshold: Decimal,
    costs: dict[str, str],
    *,
    paper_count: int,
    backtest_count: int,
    mismatches: int,
    price_delta: Decimal,
    paper_realized: Fraction,
    backtest_realized: Fraction,
) -> dict[str, Any]:
    realized_delta = paper_realized - backtest_realized
    flagged = (
        paper_count != backtest_count
        or mismatches > 0
        or price_delta > threshold
        or abs(realized_delta) > Fraction(threshold)
    )
    return {
        "strategy_id": strategy_id,
        "version": version,
        "status": "drift" if flagged else "ok",
        "flagged": flagged,
        "reason": None,
        "threshold": format_amount(threshold),
        "fill_count": {"paper": paper_count, "backtest": backtest_count},
        "side_time_mismatches": mismatches,
        "price_delta": format_amount(price_delta),
        "realized_pnl": {
            "paper": format_amount(paper_realized),
            "backtest": format_amount(backtest_realized),
            "delta": format_amount(realized_delta),
        },
        "costs": costs,
    }


def _insufficient(
    strategy_id: str,
    version: int,
    reason: str,
    threshold: Decimal,
    costs: dict[str, str] | None,
) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "version": version,
        "status": "insufficient_data",
        "flagged": False,
        "reason": reason,
        "threshold": format_amount(threshold),
        "fill_count": None,
        "side_time_mismatches": None,
        "price_delta": None,
        "realized_pnl": None,
        "costs": costs,
    }


def _document(text: str) -> dict[str, Any] | None:
    try:
        loaded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _costs(document: Mapping[str, Any]) -> dict[str, str] | None:
    raw = document.get("costs")
    if not isinstance(raw, dict):
        return None
    commission = _plain(raw.get("commission_bps"))
    slippage = _plain(raw.get("slippage_bps"))
    if commission is None or slippage is None:
        return None
    return {"commission_bps": commission, "slippage_bps": slippage}


def _plain(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str, float)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _assets(document: Mapping[str, Any]) -> list[tuple[str, str]] | None:
    raw = document.get("assets")
    if not isinstance(raw, list) or not raw:
        return None
    found: list[tuple[str, str]] = []
    for asset in raw:
        if not isinstance(asset, dict):
            return None
        symbol = asset.get("symbol")
        asset_id = asset.get("id")
        if not isinstance(symbol, str) or not isinstance(asset_id, str) or asset_id == "":
            return None
        known = BY_SYMBOL.get(symbol)
        if known is None:
            return None
        found.append((asset_id, known.mint))
    return found


def _bar_size(document: Mapping[str, Any]) -> str | None:
    engine = document.get("engine")
    if not isinstance(engine, dict):
        return None
    size = engine.get("bar_size")
    if not isinstance(size, str) or size not in TIMEFRAME_SECONDS:
        return None
    return size


def _warmup_bars(document: Mapping[str, Any]) -> int | None:
    engine = document.get("engine")
    if not isinstance(engine, dict):
        return None
    raw = engine.get("warmup_bars")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _gated(document: dict[str, Any], first_index: int) -> dict[str, Any]:
    """Pin the costs and signals. Delay the first fill until this registration.

    The broker replays earlier bars for indicator memory and writes no fill
    on a bar that closed at or before ``registered_at``. Raising the warmup
    gate to that first bar makes ``run_backtest`` do the same. The stored
    document is not written.
    """
    engine = document.get("engine")
    if not isinstance(engine, dict):
        return document
    current = engine.get("warmup_bars")
    if isinstance(current, bool) or not isinstance(current, int) or first_index <= current:
        return document
    copied = dict(document)
    copied_engine = dict(engine)
    copied_engine["warmup_bars"] = first_index
    copied["engine"] = copied_engine
    return copied


def _closed_after(open_time: int, step: int, registered_at: int) -> bool:
    return open_time + step > registered_at


def _prior_book(
    fills: Sequence[tuple[object, ...]],
    positions: Sequence[tuple[object, ...]],
    registered_at: int,
    step: int,
) -> bool:
    """True when this registration inherited a fill or an open position."""
    for fill in fills:
        if not _closed_after(int(fill[0]), step, registered_at):
            return True
    for row in positions:
        if row[1] is None or row[10] is None:
            continue
        if not _closed_after(int(row[10]), step, registered_at):
            return True
    return False


def _window(
    store: SqliteBarStore,
    user_id: str,
    assets: Sequence[tuple[str, str]],
    bar_size: str,
    registered_at: int,
    strategy_id: str,
    version: int,
    warmup: int,
) -> tuple[dict[str, list[Bar]] | None, str | None, int]:
    """Bars from the oldest retained open through the cursor, or a gap reason.

    The third value is the index of the first bar that closed after
    registration. ``(None, None, 0)`` means the cursor has not reached such
    a bar. ``(None, reason, 0)`` means the retained bars do not cover the
    span the broker replayed. A series is returned only when every slot
    from that oldest open through the cursor is stored.
    """
    step = TIMEFRAME_SECONDS[bar_size]
    cursor = store.paper_cursor(user_id=user_id, strategy_id=strategy_id, version=version)
    if cursor is None:
        return None, None, 0
    trade_open = _first_open(registered_at, step, cursor)
    if trade_open is None:
        return None, None, 0
    loaded: dict[str, dict[int, Bar]] = {}
    earliest: int | None = None
    for asset_id, mint in assets:
        rows = store.load(
            user_id=user_id,
            mint=mint,
            timeframe=bar_size,
            start=0,
            end=cursor + step,
        )
        by_time = {bar.open_time: bar for bar in rows if bar.open_time <= cursor}
        loaded[asset_id] = by_time
        if by_time:
            first = min(by_time)
            earliest = first if earliest is None else min(earliest, first)
    if earliest is None:
        return None, "window_before_retained", 0
    # The broker fixes its indicator origin at the first tick. Bars stored
    # later with an older open time are not part of that window. Starting
    # here keeps the backtest on the same seed, so a backfill cannot flag
    # a false drift. A purge that drops the origin bar still starts at the
    # oldest bar that remains: the EMA reseeds after that gap.
    recorded = store.paper_origin(
        user_id=user_id, strategy_id=strategy_id, version=version
    )
    if (
        recorded is not None
        and earliest < recorded <= cursor
        and (recorded - earliest) % step == 0
    ):
        earliest = recorded
    if (cursor - earliest) % step != 0 or (trade_open - earliest) % step != 0:
        return None, "bar_missed", 0
    expected = list(range(earliest, cursor + 1, step))
    trade_slots = list(range(trade_open, cursor + 1, step))
    series: dict[str, list[Bar]] = {}
    saw_trade = False
    missing = False
    for asset_id, by_time in loaded.items():
        if any(open_time in by_time for open_time in trade_slots):
            saw_trade = True
        if any(open_time not in by_time for open_time in expected):
            missing = True
            continue
        series[asset_id] = [by_time[open_time] for open_time in expected]
    if missing:
        reason = "window_before_retained" if not saw_trade else "bar_missed"
        return None, reason, 0
    if not expected:
        return None, None, 0
    if warmup > 0 and len(expected) < warmup:
        return None, "short_history", 0
    return series, None, (trade_open - earliest) // step


def _first_open(registered_at: int, step: int, cursor: int) -> int | None:
    """First aligned open whose close is after ``registered_at``, at or before the cursor."""
    if step < 1 or cursor < 0:
        return None
    start = registered_at - step + 1
    if start < 0:
        start = 0
    remainder = start % step
    first = start if remainder == 0 else start + (step - remainder)
    if first > cursor:
        return None
    return first


def _fills_cover_book(
    wallet: tuple[str, str] | None,
    positions: Sequence[tuple[object, ...]],
    fills: Sequence[tuple[object, ...]],
) -> bool:
    """True when replaying the retained fills lands on the stored cash and qty.

    A false result means the fill log does not cover the durable book. The
    book is left as stored. It is not rebuilt from the remaining fills.
    """
    try:
        if wallet is None:
            return not fills and all(row[1] is None for row in positions)
        replay = Fraction(Decimal(wallet[1]))
        cash = Fraction(Decimal(wallet[0]))
        qty: dict[str, Fraction] = {}
        for fill in fills:
            asset_id = str(fill[1])
            amount = Fraction(Decimal(str(fill[3])))
            notional = Fraction(Decimal(str(fill[5])))
            commission = Fraction(Decimal(str(fill[6])))
            held = qty.get(asset_id, Fraction(0))
            if fill[2] == "buy":
                replay -= notional + commission
                qty[asset_id] = held + amount
            elif fill[2] == "sell":
                replay += notional - commission
                qty[asset_id] = held - amount
            else:
                return False
        present = {asset_id: amount for asset_id, amount in qty.items() if amount != 0}
        wanted = {
            str(row[0]): Fraction(Decimal(str(row[1])))
            for row in positions
            if row[1] is not None
        }
    except (InvalidOperation, ValueError, ArithmeticError):
        return False
    return replay == cash and present == wanted


def _paper_fill(row: tuple[object, ...]) -> _Fill:
    return _Fill(int(row[0]), str(row[1]), str(row[2]), Decimal(str(row[4])))


def _paper_realized(positions: Sequence[tuple[object, ...]]) -> Fraction:
    total = Fraction(0)
    for row in positions:
        total += _fraction(str(row[5]))
    return total


def _backtest_realized(trades: Sequence[Any]) -> Fraction:
    total = Fraction(0)
    for trade in trades:
        total += Fraction(trade.realized_pnl)
    return total


def _fraction(text: str) -> Fraction:
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        return Fraction(int(numerator), int(denominator))
    return Fraction(Decimal(text))


def _pair(paper: Sequence[_Fill], backtest: Sequence[_Fill]) -> tuple[int, Decimal]:
    """Side/time mismatches, and the widest absolute price gap among pairs."""
    left = sorted(paper, key=_key)
    right = sorted(backtest, key=_key)
    index_left = 0
    index_right = 0
    mismatches = 0
    widest = Decimal(0)
    while index_left < len(left) or index_right < len(right):
        both = index_left < len(left) and index_right < len(right)
        if both and _key(left[index_left]) == _key(right[index_right]):
            gap = abs(left[index_left].price - right[index_right].price)
            if gap > widest:
                widest = gap
            index_left += 1
            index_right += 1
            continue
        mismatches += 1
        if index_right >= len(right) or (
            index_left < len(left) and _key(left[index_left]) < _key(right[index_right])
        ):
            index_left += 1
        else:
            index_right += 1
    return mismatches, widest


def _key(fill: _Fill) -> tuple[int, str, str]:
    return (fill.open_time, fill.asset_id, fill.side)


def _display_context() -> Context:
    ctx = Context(
        prec=50,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        traps=[InvalidOperation, DivisionByZero, Overflow],
        clamp=0,
    )
    return ctx
