"""Apply the backtester's fills to a durable paper book.

One registered strategy version has one cash balance and at most one open
order. Fills use ``app.backtest.engine`` on the closed bar that produced the
signal (``on_close``). Bars that closed at or before registration are stepped
for indicator memory and are not filled. Positions are read back from the
position rows before each bar. They are not rebuilt from ``live_decisions``
or from that memory replay.

The order lifetime is ``ORDER_TTL_SECONDS`` (300) after the bar's close, not
after its open and not stretched to the bar size. A tick that is still inside
that window fills. A later tick expires the order and does not fill it. The
scheduler's close grace is 60 seconds, so a prompt tick fills. A daily bar
does not get a longer life: the five minutes start when that bar closes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any

from app.auth import LOCAL_USER_ID
from app.backtest.engine import (
    _Acc,
    _apply_decision,
    _Book,
    _dec,
    _engine_context,
    _Oco,
    _Position,
)
from app.backtest.errors import BacktestError
from app.backtest.money import quantize_cents
from app.live.feed import last_closed_open
from app.ohlcv.bars import TIMEFRAME_SECONDS, Bar
from app.ohlcv.registry import BY_SYMBOL
from app.ohlcv.store import SqliteBarStore
from app.paper.window import build_window
from app.runner import (
    BarDecision,
    BookView,
    OcoLevels,
    OcoTouch,
    RunnerState,
    config_from_document,
    initial_state,
)
from app.runner import step as decide_bar

logger = logging.getLogger("app.paper")

# Five minutes after the bar closes. Not five bars, and not the bar's length.
ORDER_TTL_SECONDS = 300

_BPS = Decimal(10000)


@dataclass
class _Held:
    """Lifetime booked realized, and the open trade's share of it."""

    lifetime: Fraction
    trade: Fraction


@dataclass(frozen=True, slots=True)
class _Costs:
    initial: Decimal
    initial_text: str
    commission_bps: Decimal
    slippage_bps: Decimal
    allow_pyramiding: bool
    max_positions: int
    actions: dict[str, Any]


def reconcile_book(
    *,
    cash: str,
    initial: str,
    positions: Sequence[tuple[object, ...]],
    fills: Sequence[tuple[object, ...]],
    marks: Mapping[str, Decimal],
) -> tuple[bool, bool]:
    """``(identity_holds, fill_replay_matches)``.

    The identity reads the wallet and the position rows. ``marks`` are closes
    the caller still has; they are not stored on the row. Fill replay starts
    from initial capital. A false replay does not change the rows. That is
    the result after the fill log has been purged.
    """
    exact = Fraction(0)
    equity = _fraction_decimal(cash)
    for row in positions:
        exact += _fraction(str(row[5]))
        if row[1] is None:
            continue
        mark = marks.get(str(row[0]))
        if mark is None or row[4] is None:
            return False, False
        qty = _fraction_decimal(str(row[1]))
        exact += qty * Fraction(mark) - _fraction(str(row[4]))
        equity += qty * Fraction(mark)
    identity = exact == equity - _fraction_decimal(initial)
    replay_cash = _fraction_decimal(initial)
    replay_qty: dict[str, Fraction] = {}
    for fill in fills:
        asset_id = str(fill[1])
        qty = _fraction_decimal(str(fill[3]))
        notional = _fraction_decimal(str(fill[5]))
        commission = _fraction_decimal(str(fill[6]))
        held_qty = replay_qty.get(asset_id, Fraction(0))
        if fill[2] == "buy":
            replay_cash -= notional + commission
            replay_qty[asset_id] = held_qty + qty
        else:
            replay_cash += notional - commission
            replay_qty[asset_id] = held_qty - qty
    wanted = {
        str(row[0]): _fraction_decimal(str(row[1]))
        for row in positions
        if row[1] is not None
    }
    present = {asset_id: qty for asset_id, qty in replay_qty.items() if qty != 0}
    replay = replay_cash == _fraction_decimal(cash) and present == wanted
    return identity, replay


class PaperBroker:
    """Tick a registered version forward from its paper cursor.

    Runner memory stays in this process. It is not written down: the samples
    are prices. A new broker rebuilds that memory by stepping bars it has
    already passed, and it does not apply those steps to the book.
    """

    def __init__(
        self,
        store: SqliteBarStore,
        *,
        now: Callable[[], int] | None = None,
        user_id: str = LOCAL_USER_ID,
    ) -> None:
        self._store = store
        self._now = (lambda: int(time.time())) if now is None else now
        self._user_id = user_id
        self._runner: dict[tuple[str, int], RunnerState] = {}
        self._marks: dict[tuple[str, int], dict[str, Decimal]] = {}
        self._origin: dict[tuple[str, int], int] = {}
        self._stepped: dict[tuple[str, int], int | None] = {}

    def tick(self) -> None:
        """Fill new closed bars for every registration. Decisions are not read."""
        now = int(self._now())
        rows = self._store.live_registrations(user_id=self._user_id)
        for strategy_id, version, document_json, _bar_size in rows:
            try:
                document = json.loads(document_json)
                self._tick_one(strategy_id, version, document, now)
            except Exception:
                logger.exception(
                    "paper tick failed strategy_id=%s version=%s",
                    strategy_id,
                    version,
                )

    def _tick_one(
        self, strategy_id: str, version: int, document: Mapping[str, Any], now: int
    ) -> None:
        bar_size = str(document["engine"]["bar_size"])
        step_seconds = TIMEFRAME_SECONDS.get(bar_size)
        if step_seconds is None:
            return
        registered_at = self._store.live_registered_at(
            user_id=self._user_id, strategy_id=strategy_id, version=version
        )
        if registered_at is None:
            return
        assets = _assets(document)
        if assets is None:
            return
        costs = _costs(document)
        if costs is None:
            return
        closed_through = last_closed_open(now, step_seconds)
        if closed_through is None:
            return
        bars = self._load_bars(assets, bar_size, closed_through + step_seconds)
        opens = [bar.open_time for by_time in bars.values() for bar in by_time.values()]
        if not opens:
            return
        key = (strategy_id, version)
        origin = self._origin.setdefault(key, min(opens))
        asset_ids = tuple(asset_id for asset_id, _mint in assets)
        config = config_from_document(document)
        cursor = self._store.paper_cursor(
            user_id=self._user_id, strategy_id=strategy_id, version=version
        )
        if key not in self._stepped or self._stepped[key] != cursor:
            book, _open_acc, _held = self._load_book(strategy_id, version, costs)
            state, marks = _replay_memory(
                document, config, asset_ids, bars, origin, step_seconds, cursor, book
            )
            self._runner[key] = state
            self._marks[key] = marks
            self._stepped[key] = cursor
        start = origin if cursor is None else max(cursor + step_seconds, origin)
        expire_ids, expire_events = self._due_orders(strategy_id, version, now)
        blocked = self._blocked(strategy_id, version, now)
        slot = start
        while slot <= closed_through:
            self._runner[key] = self._one_slot(
                strategy_id,
                version,
                document,
                config,
                asset_ids,
                bars,
                origin,
                slot,
                step_seconds,
                now,
                registered_at,
                costs,
                blocked,
                expire_ids,
                expire_events,
            )
            expire_ids = ()
            expire_events = []
            self._stepped[key] = slot
            slot += step_seconds
        if expire_ids and cursor is not None:
            self._commit(
                strategy_id,
                version,
                costs,
                cursor,
                positions=[],
                orders=[],
                fills=[],
                events=expire_events,
                expire_order_ids=expire_ids,
                cash=self._cash_text(strategy_id, version, costs),
            )

    def _one_slot(
        self,
        strategy_id: str,
        version: int,
        document: Mapping[str, Any],
        config: Any,
        asset_ids: Sequence[str],
        bars: Mapping[str, Mapping[int, Bar]],
        origin: int,
        open_time: int,
        step_seconds: int,
        now: int,
        registered_at: int,
        costs: _Costs,
        blocked: bool,
        expire_ids: Sequence[int],
        expire_events: Sequence[Mapping[str, object]],
    ) -> RunnerState:
        key = (strategy_id, version)
        marks = self._marks.setdefault(key, {})
        _note_marks(marks, bars, open_time)
        window = build_window(document, asset_ids, bars, origin, open_time, step_seconds)
        book, open_acc, held = self._load_book(strategy_id, version, costs)
        outcome = decide_bar(config, self._runner[key], window, _view(book))
        close_time = open_time + step_seconds
        if close_time <= registered_at:
            self._commit(
                strategy_id,
                version,
                costs,
                open_time,
                positions=[],
                orders=[],
                fills=[],
                events=expire_events,
                expire_order_ids=expire_ids,
                cash=_money(book.cash),
            )
            return outcome.state
        decision = outcome.decision
        real = _has_real(decision)
        late = now > close_time + ORDER_TTL_SECONDS
        if real and (late or blocked):
            orders, events = _refuse(decision, document, open_time, close_time, blocked)
            self._commit(
                strategy_id,
                version,
                costs,
                open_time,
                positions=[],
                orders=orders,
                fills=[],
                events=[*expire_events, *events],
                expire_order_ids=expire_ids,
                cash=_money(book.cash),
            )
            return outcome.state
        positions, orders, fills, events = _apply_once(
            decision, book, open_acc, held, marks, open_time, close_time, costs, document
        )
        self._commit(
            strategy_id,
            version,
            costs,
            open_time,
            positions=positions,
            orders=orders,
            fills=fills,
            events=[*expire_events, *events],
            expire_order_ids=expire_ids,
            cash=_money(book.cash),
        )
        return outcome.state

    def _load_book(
        self, strategy_id: str, version: int, costs: _Costs
    ) -> tuple[_Book, dict[str, _Acc], dict[str, _Held]]:
        wallet = self._store.paper_wallet(
            user_id=self._user_id, strategy_id=strategy_id, version=version
        )
        rows = self._store.paper_positions(
            user_id=self._user_id, strategy_id=strategy_id, version=version
        )
        cash = costs.initial if wallet is None else Decimal(wallet[0])
        return _book_from_rows(cash, rows)

    def _due_orders(
        self, strategy_id: str, version: int, now: int
    ) -> tuple[tuple[int, ...], list[dict[str, object]]]:
        ids: list[int] = []
        events: list[dict[str, object]] = []
        for order_id, open_time, asset_id, expires_at in self._store.paper_open_orders(
            user_id=self._user_id, strategy_id=strategy_id, version=version
        ):
            if expires_at > now:
                continue
            ids.append(order_id)
            events.append(
                _event(open_time, "ORDER_EXPIRED", {"asset_id": asset_id, "reason": "ttl"})
            )
        return tuple(ids), events

    def _blocked(self, strategy_id: str, version: int, now: int) -> bool:
        return any(
            expires_at > now
            for _order_id, _open_time, _asset_id, expires_at in self._store.paper_open_orders(
                user_id=self._user_id, strategy_id=strategy_id, version=version
            )
        )

    def _cash_text(self, strategy_id: str, version: int, costs: _Costs) -> str:
        wallet = self._store.paper_wallet(
            user_id=self._user_id, strategy_id=strategy_id, version=version
        )
        if wallet is None:
            return costs.initial_text
        return wallet[0]

    def _commit(
        self,
        strategy_id: str,
        version: int,
        costs: _Costs,
        cursor_open_time: int,
        *,
        positions: Sequence[Mapping[str, object]],
        orders: Sequence[Mapping[str, object]],
        fills: Sequence[Mapping[str, object]],
        events: Sequence[Mapping[str, object]],
        expire_order_ids: Sequence[int],
        cash: str,
    ) -> None:
        self._store.commit_paper_step(
            user_id=self._user_id,
            strategy_id=strategy_id,
            version=version,
            cash=cash,
            initial_capital=costs.initial_text,
            positions=positions,
            orders=orders,
            fills=fills,
            events=events,
            cursor_open_time=cursor_open_time,
            expire_order_ids=expire_order_ids,
        )

    def _load_bars(
        self, assets: Sequence[tuple[str, str]], bar_size: str, end: int
    ) -> dict[str, dict[int, Bar]]:
        loaded: dict[str, dict[int, Bar]] = {}
        for asset_id, mint in assets:
            rows = self._store.load(
                user_id=self._user_id,
                mint=mint,
                timeframe=bar_size,
                start=0,
                end=end,
            )
            loaded[asset_id] = {bar.open_time: bar for bar in rows}
        return loaded


def _replay_memory(
    document: Mapping[str, Any],
    config: Any,
    asset_ids: Sequence[str],
    bars: Mapping[str, Mapping[int, Bar]],
    origin: int,
    step_seconds: int,
    cursor: int | None,
    book: _Book,
) -> tuple[RunnerState, dict[str, Decimal]]:
    """Step through ``cursor`` for memory only. No order is written."""
    state = initial_state(asset_ids)
    marks: dict[str, Decimal] = {}
    if cursor is None or cursor < origin:
        return state, marks
    slot = origin
    while slot <= cursor:
        _note_marks(marks, bars, slot)
        window = build_window(document, asset_ids, bars, origin, slot, step_seconds)
        view = BookView() if not _visible(book, slot) else _view(book)
        state = decide_bar(config, state, window, view).state
        slot += step_seconds
    return state, marks


def _apply_once(
    decision: BarDecision,
    book: _Book,
    open_acc: dict[str, _Acc],
    held: dict[str, _Held],
    marks: dict[str, Decimal],
    open_time: int,
    close_time: int,
    costs: _Costs,
    document: Mapping[str, Any],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    fills: list[Any] = []
    skipped: list[Any] = []
    lints: list[str] = []
    closed: list[_Acc] = []
    with localcontext(_engine_context()):
        _apply_decision(
            decision,
            costs.actions,
            book,
            marks,
            open_acc,
            closed,
            fills,
            skipped,
            lints,
            set(),
            open_time,
            allow_pyramiding=costs.allow_pyramiding,
            max_positions=costs.max_positions,
            commission_bps=costs.commission_bps,
            slippage_bps=costs.slippage_bps,
        )
        positions = _snapshot(book, open_acc, closed, held)
        orders, events = _filled_events(
            decision, document, fills, skipped, open_time, close_time
        )
        for text in lints:
            events.append(_event(open_time, "LINT", {"message": text}))
        fill_rows = [_fill_row(fill) for fill in fills]
    return positions, orders, fill_rows, events


def _filled_events(
    decision: BarDecision,
    document: Mapping[str, Any],
    fills: Sequence[Any],
    skipped: Sequence[Any],
    open_time: int,
    close_time: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    actions = {str(action["id"]): action for action in document["actions"]}
    rules = {str(rule["do"]): rule for rule in document["rules"]}
    orders: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    fill_at = 0
    skip_at = 0
    for item in decision.oco:
        if isinstance(item, OcoTouch):
            if fill_at < len(fills) and fills[fill_at].asset_id == item.asset_id:
                fill = fills[fill_at]
                fill_at += 1
                orders.append(_order_from_fill(fill, close_time, "filled", ""))
                events.extend(_place_fill(fill, open_time, close_time, None))
            else:
                events.append(
                    _event(
                        open_time,
                        "ORDER_REJECTED",
                        {"asset_id": item.asset_id, "leg": item.leg, "reason": "no_position"},
                    )
                )
    for attempt in decision.rules:
        rule = rules[attempt.action_id]
        events.append(
            _event(
                open_time,
                "SIGNAL_FIRED",
                {
                    "action_id": attempt.action_id,
                    "asset_id": attempt.asset_id,
                    "signal_id": str(rule["when"]),
                },
            )
        )
        if attempt.skip is not None:
            if skip_at < len(skipped) and skipped[skip_at].action_id == attempt.action_id:
                skip_at += 1
            orders.append(
                _bare_order(
                    open_time,
                    close_time,
                    attempt.asset_id,
                    _side(actions[attempt.action_id]),
                    "rejected",
                    "market",
                    attempt.skip,
                )
            )
            events.append(
                _event(
                    open_time,
                    "ORDER_REJECTED",
                    {
                        "action_id": attempt.action_id,
                        "asset_id": attempt.asset_id,
                        "reason": attempt.skip,
                    },
                )
            )
            continue
        if skip_at < len(skipped) and skipped[skip_at].action_id == attempt.action_id:
            reason = str(skipped[skip_at].reason)
            skip_at += 1
            orders.append(
                _bare_order(
                    open_time,
                    close_time,
                    attempt.asset_id,
                    _side(actions[attempt.action_id]),
                    "rejected",
                    "market",
                    reason,
                )
            )
            events.append(
                _event(
                    open_time,
                    "ORDER_REJECTED",
                    {
                        "action_id": attempt.action_id,
                        "asset_id": attempt.asset_id,
                        "reason": reason,
                    },
                )
            )
            continue
        fill = fills[fill_at]
        fill_at += 1
        orders.append(_order_from_fill(fill, close_time, "filled", ""))
        events.extend(_place_fill(fill, open_time, close_time, attempt.action_id))
    return orders, events


def _refuse(
    decision: BarDecision,
    document: Mapping[str, Any],
    open_time: int,
    close_time: int,
    blocked: bool,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Do not fill. A late bar expires. A second order is rejected."""
    actions = {str(action["id"]): action for action in document["actions"]}
    rules = {str(rule["do"]): rule for rule in document["rules"]}
    reason = "open_order" if blocked else "ttl"
    status = "rejected" if blocked else "expired"
    kind = "ORDER_REJECTED" if blocked else "ORDER_EXPIRED"
    orders: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    for item in decision.oco:
        if not isinstance(item, OcoTouch):
            continue
        orders.append(
            _bare_order(open_time, close_time, item.asset_id, "sell", status, item.leg, reason)
        )
        if not blocked:
            events.append(
                _event(
                    open_time,
                    "ORDER_PLACED",
                    {"asset_id": item.asset_id, "leg": item.leg, "side": "sell"},
                )
            )
        events.append(
            _event(open_time, kind, {"asset_id": item.asset_id, "leg": item.leg, "reason": reason})
        )
    for attempt in decision.rules:
        rule = rules[attempt.action_id]
        events.append(
            _event(
                open_time,
                "SIGNAL_FIRED",
                {
                    "action_id": attempt.action_id,
                    "asset_id": attempt.asset_id,
                    "signal_id": str(rule["when"]),
                },
            )
        )
        order_status = "rejected" if attempt.skip is not None or blocked else "expired"
        order_reason = attempt.skip if attempt.skip is not None else reason
        event_kind = "ORDER_REJECTED" if order_status == "rejected" else "ORDER_EXPIRED"
        orders.append(
            _bare_order(
                open_time,
                close_time,
                attempt.asset_id,
                _side(actions[attempt.action_id]),
                order_status,
                "market",
                order_reason,
            )
        )
        if order_status == "expired":
            events.append(
                _event(
                    open_time,
                    "ORDER_PLACED",
                    {
                        "action_id": attempt.action_id,
                        "asset_id": attempt.asset_id,
                        "side": _side(actions[attempt.action_id]),
                    },
                )
            )
        events.append(
            _event(
                open_time,
                event_kind,
                {
                    "action_id": attempt.action_id,
                    "asset_id": attempt.asset_id,
                    "reason": order_reason,
                },
            )
        )
    return orders, events


def _snapshot(
    book: _Book,
    open_acc: Mapping[str, _Acc],
    closed: Sequence[_Acc],
    held: Mapping[str, _Held],
) -> list[dict[str, object]]:
    """One row per asset. A same-bar re-entry stays open.

    An OCO fill closes the old account and a later enter on that bar opens a
    new one. The closed account's realized has to land on the open row. A
    flat row for that asset would be applied second and wipe the new long.
    """
    booked = _closed_realized(closed)
    rows: list[dict[str, object]] = []
    open_ids = set(open_acc)
    for asset_id, acc in open_acc.items():
        lifetime = _lifetime(held.get(asset_id), acc) + booked.get(asset_id, Fraction(0))
        rows.append(_open_row(asset_id, book, acc, lifetime))
    for acc in closed:
        if acc.asset_id in open_ids:
            continue
        previous = held.get(acc.asset_id)
        base = Fraction(0) if previous is None else previous.lifetime - previous.trade
        rows.append(_flat_row(acc.asset_id, base + acc.realized_exact))
    return rows


def _closed_realized(closed: Sequence[_Acc]) -> dict[str, Fraction]:
    booked: dict[str, Fraction] = {}
    for acc in closed:
        booked[acc.asset_id] = booked.get(acc.asset_id, Fraction(0)) + acc.realized_exact
    return booked


def _open_row(asset_id: str, book: _Book, acc: _Acc, lifetime: Fraction) -> dict[str, object]:
    position = book.positions[asset_id]
    oco = book.ocos.get(asset_id)
    return {
        "asset_id": asset_id,
        "qty": _money(position.qty),
        "open_cost": _money(position.cost),
        "entry": _money(position.entry),
        "basis": _fraction_text(acc.basis),
        "realized": _fraction_text(lifetime),
        "trade_realized": _fraction_text(acc.realized_exact),
        "take_profit": None if oco is None else _money(oco.take_profit),
        "stop_loss": None if oco is None else _money(oco.stop_loss),
        "anchor": None if oco is None else _money(oco.anchor),
        "attached_at": None if oco is None else oco.attached_at,
    }


def _flat_row(asset_id: str, lifetime: Fraction) -> dict[str, object]:
    """Price fields are null once the long is flat. Realized stays."""
    return {
        "asset_id": asset_id,
        "qty": None,
        "open_cost": None,
        "entry": None,
        "basis": None,
        "realized": _fraction_text(lifetime),
        "trade_realized": None,
        "take_profit": None,
        "stop_loss": None,
        "anchor": None,
        "attached_at": None,
    }


def _lifetime(previous: _Held | None, acc: _Acc) -> Fraction:
    base = Fraction(0) if previous is None else previous.lifetime - previous.trade
    return base + acc.realized_exact


def _book_from_rows(
    cash: Decimal, rows: Sequence[tuple[object, ...]]
) -> tuple[_Book, dict[str, _Acc], dict[str, _Held]]:
    book = _Book(cash=cash, positions={}, ocos={})
    open_acc: dict[str, _Acc] = {}
    held: dict[str, _Held] = {}
    for row in rows:
        asset_id = str(row[0])
        trade = Fraction(0) if row[6] is None else _fraction(str(row[6]))
        held[asset_id] = _Held(_fraction(str(row[5])), trade)
        if row[1] is None:
            continue
        qty = Decimal(str(row[1]))
        cost = Decimal(str(row[2]))
        entry = Decimal(str(row[3]))
        book.positions[asset_id] = _Position(qty, cost, entry)
        if row[7] is not None and row[8] is not None and row[9] is not None and row[10] is not None:
            book.ocos[asset_id] = _Oco(
                Decimal(str(row[7])),
                Decimal(str(row[8])),
                int(row[10]),
                Decimal(str(row[9])),
            )
        acc = _Acc(asset_id, int(row[10]) if row[10] is not None else 0)
        acc.qty = qty
        acc.cost = cost
        acc.basis = _fraction(str(row[4])) if row[4] is not None else Fraction(0)
        acc.realized_exact = trade
        open_acc[asset_id] = acc
    return book, open_acc, held


def _view(book: _Book) -> BookView:
    return BookView(
        tuple((asset_id, position.entry) for asset_id, position in book.positions.items()),
        tuple(
            (asset_id, OcoLevels(oco.take_profit, oco.stop_loss, oco.attached_at))
            for asset_id, oco in book.ocos.items()
        ),
    )


def _visible(book: _Book, open_time: int) -> bool:
    """True when the long was already open before this bar's decision."""
    return any(oco.attached_at < open_time for oco in book.ocos.values())


def _has_real(decision: BarDecision) -> bool:
    if any(isinstance(item, OcoTouch) for item in decision.oco):
        return True
    return any(attempt.skip is None for attempt in decision.rules)


def _note_marks(
    marks: dict[str, Decimal], bars: Mapping[str, Mapping[int, Bar]], open_time: int
) -> None:
    for asset_id, by_time in bars.items():
        bar = by_time.get(open_time)
        if bar is not None:
            marks[asset_id] = Decimal(str(bar.close))


def _assets(document: Mapping[str, Any]) -> list[tuple[str, str]] | None:
    found: list[tuple[str, str]] = []
    for asset in document["assets"]:
        known = BY_SYMBOL.get(str(asset["symbol"]))
        if known is None:
            return None
        found.append((str(asset["id"]), known.mint))
    return found


def _costs(document: Mapping[str, Any]) -> _Costs | None:
    try:
        with localcontext(_engine_context()):
            slippage = _dec(document["costs"]["slippage_bps"])
            if slippage >= _BPS:
                raise BacktestError(
                    "slippage_bps must be less than 10000; a sell would fill at 0"
                )
            initial = quantize_cents(Fraction(_dec(document["capital"]["initial"])))
            return _Costs(
                initial=initial,
                initial_text=_money(initial),
                commission_bps=_dec(document["costs"]["commission_bps"]),
                slippage_bps=slippage,
                allow_pyramiding=bool(document["portfolio"]["allow_pyramiding"]),
                max_positions=int(document["portfolio"]["max_positions"]),
                actions={str(action["id"]): action for action in document["actions"]},
            )
    except (BacktestError, KeyError, TypeError, ValueError):
        return None


def _side(action: Mapping[str, Any]) -> str:
    return "buy" if action["type"] == "enter" else "sell"


def _order_from_fill(fill: Any, close_time: int, status: str, reason: str) -> dict[str, object]:
    return {
        "open_time": fill.open_time,
        "asset_id": fill.asset_id,
        "side": fill.side,
        "status": status,
        "placed_at": close_time,
        "expires_at": close_time + ORDER_TTL_SECONDS,
        "price": _money(fill.price),
        "qty": _money(fill.qty),
        "notional": _money(fill.notional),
        "commission": _money(fill.commission),
        "leg": fill.leg,
        "reason": reason,
    }


def _bare_order(
    open_time: int,
    close_time: int,
    asset_id: str,
    side: str,
    status: str,
    leg: str,
    reason: str,
) -> dict[str, object]:
    return {
        "open_time": open_time,
        "asset_id": asset_id,
        "side": side,
        "status": status,
        "placed_at": close_time,
        "expires_at": close_time + ORDER_TTL_SECONDS,
        "price": None,
        "qty": None,
        "notional": None,
        "commission": None,
        "leg": leg,
        "reason": reason,
    }


def _fill_row(fill: Any) -> dict[str, object]:
    return {
        "open_time": fill.open_time,
        "asset_id": fill.asset_id,
        "side": fill.side,
        "qty": _money(fill.qty),
        "price": _money(fill.price),
        "notional": _money(fill.notional),
        "commission": _money(fill.commission),
        "leg": fill.leg,
        "limit_below_low": fill.limit_below_low,
    }


def _place_fill(
    fill: Any, open_time: int, close_time: int, action_id: str | None
) -> list[dict[str, object]]:
    body: dict[str, object] = {
        "asset_id": fill.asset_id,
        "leg": fill.leg,
        "price": _money(fill.price),
        "qty": _money(fill.qty),
        "notional": _money(fill.notional),
        "commission": _money(fill.commission),
        "side": fill.side,
    }
    if action_id is not None:
        body["action_id"] = action_id
    placed = dict(body)
    placed.pop("price")
    placed.pop("qty")
    placed.pop("notional")
    placed.pop("commission")
    return [
        _event(open_time, "ORDER_PLACED", placed),
        _event(open_time, "ORDER_FILLED", body),
    ]


def _event(open_time: int, kind: str, detail: Mapping[str, object]) -> dict[str, object]:
    return {
        "open_time": open_time,
        "kind": kind,
        "detail": json.dumps(detail, sort_keys=True, separators=(",", ":")),
    }


def _money(value: Decimal) -> str:
    return format(value, "f")


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _fraction(text: str) -> Fraction:
    numerator, denominator = text.split("/", 1)
    return Fraction(int(numerator), int(denominator))


def _fraction_decimal(text: str) -> Fraction:
    return Fraction(Decimal(text))
