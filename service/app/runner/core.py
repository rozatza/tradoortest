"""Pure per-bar decisions for one canonical strategy.

Bars and state come in. A decision and a new state go out. The input state
is not modified. Nothing here reads the clock, draws a random number, touches
the network, or opens a file. There is no module-level mutable global.

A decision on bar ``t`` receives a window whose series each have length
``t + 1``. Index ``t + 1`` does not exist on that window. Fills are not
applied here. The caller applies the decision, then passes the new book view
on the next bar. ``run`` is that loop with the views supplied up front, so
stepping one bar at a time matches a batch of the same views.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.backtest.errors import BacktestError
from app.backtest.eval import EvalContext, EvalMemory, evaluate
from app.ohlcv.bars import Bar


class ClosedSeries(Sequence[float]):
    """Bars ``0 .. end-1`` of a longer buffer. Index ``end`` raises.

    The tail stays on the buffer the caller owns. This sequence does not
    return it and does not offer a public handle to it. A slice is bars
    ``0 .. end-1``. One that would read index ``end`` raises.
    """

    __slots__ = ("_end", "_values")

    def __init__(self, values: Sequence[float], end: int) -> None:
        if isinstance(end, bool) or not isinstance(end, int) or end < 0:
            raise BacktestError("closed window end is not a bar index")
        if end > len(values):
            raise BacktestError("closed window is missing a bar it claims to include")
        self._values = values
        self._end = end

    def __len__(self) -> int:
        return self._end

    def __getitem__(self, index: int | slice) -> float | tuple[float, ...]:
        if isinstance(index, slice):
            return self._slice(index)
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("series index must be an integer")
        if index < 0:
            index += self._end
        if index < 0 or index >= self._end:
            raise IndexError("bar is outside the closed window")
        return self._values[index]

    def _slice(self, index: slice) -> tuple[float, ...]:
        """Bars inside the closed end. Index ``end`` is bar ``t + 1`` and raises.

        Negative bounds count back from ``end``, as an index does. Defaults
        follow ``slice.indices``. A step that would visit ``end`` raises
        instead of reading the buffer tail.
        """
        if index.step == 0:
            raise ValueError("slice step cannot be zero")
        end = self._end
        step = 1 if index.step is None else index.step
        if isinstance(step, bool) or not isinstance(step, int):
            raise TypeError("series index must be an integer")
        start = self._slice_bound(index.start, end)
        stop = self._slice_bound(index.stop, end)
        if step > 0:
            if start is None:
                start = 0
            if stop is None:
                stop = end
        else:
            if start is None:
                start = end - 1
            if stop is None:
                stop = -1
        picked = list(range(start, stop, step))
        if any(i < 0 or i >= end for i in picked):
            raise IndexError("bar is outside the closed window")
        return tuple(self._values[i] for i in picked)

    @staticmethod
    def _slice_bound(bound: int | None, end: int) -> int | None:
        if bound is None:
            return None
        if isinstance(bound, bool) or not isinstance(bound, int):
            raise TypeError("series index must be an integer")
        resolved = bound + end if bound < 0 else bound
        if resolved < 0 or resolved > end:
            raise IndexError("bar is outside the closed window")
        return resolved


@dataclass(frozen=True, slots=True)
class MemoryState:
    """Cross and bars-since memory for one asset. Tuples, not shared dicts."""

    cross_prev: tuple[tuple[tuple[str, ...], tuple[float | None, float | None]], ...] = ()
    since: tuple[tuple[tuple[str, ...], int | None], ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerState:
    """Per-asset memory after the bars already decided. Insertion order is asset order."""

    memory: tuple[tuple[str, MemoryState], ...]


@dataclass(frozen=True, slots=True)
class OcoLevels:
    """The pair attached to an open long, as it stands before this bar."""

    take_profit: Decimal
    stop_loss: Decimal
    attached_at: int


@dataclass(frozen=True, slots=True)
class BookView:
    """What the decision may know about the book. It is not a book.

    ``entries`` is ``(asset id, average fill)`` for longs that are open.
    ``ocos`` is the pair attached to each of those assets, if it has one.
    """

    entries: tuple[tuple[str, Decimal], ...] = ()
    ocos: tuple[tuple[str, OcoLevels], ...] = ()


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """The IR fields a decision reads. Built once, then reused for every bar."""

    asset_ids: tuple[str, ...]
    warmup_bars: int
    signals: tuple[tuple[str, Mapping[str, Any]], ...]
    action_asset: tuple[tuple[str, str], ...]
    rules: tuple[Mapping[str, Any], ...]
    pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class BarWindow:
    """Bar ``t`` and the series prefix ``0..t``. There is no bar ``t + 1``."""

    t: int
    open_time: int
    series: Mapping[str, Mapping[str, ClosedSeries]]
    bars: Mapping[str, Bar | None]


@dataclass(frozen=True, slots=True)
class SignalTruth:
    """One signal on one asset, from the book as it stood before this bar."""

    signal_id: str
    asset_id: str
    is_true: bool


@dataclass(frozen=True, slots=True)
class OcoDrop:
    """An attached pair whose long is already flat. The caller drops it."""

    asset_id: str


@dataclass(frozen=True, slots=True)
class OcoTouch:
    """A leg touched by this bar's own high and low. The caller simulates the fill."""

    asset_id: str
    leg: str
    raw_price: Decimal
    limit_below_low: bool


@dataclass(frozen=True, slots=True)
class RuleAttempt:
    """A rule whose signal is true.

    ``skip`` is ``missing_bar`` or ``synthetic_bar`` when this bar cannot
    fill. Otherwise ``close`` is this bar's close and the caller tries the order.
    """

    action_id: str
    asset_id: str
    skip: str | None
    close: Decimal | None


@dataclass(frozen=True, slots=True)
class BarDecision:
    """What bar ``t`` decided, before the book changes.

    OCO actions are in asset order. Rules are in IR order and only include a
    true signal. Both are empty during warmup. Signals are still listed, so
    the memory in the new state has seen this bar.
    """

    signals: tuple[SignalTruth, ...]
    oco: tuple[OcoDrop | OcoTouch, ...]
    rules: tuple[RuleAttempt, ...]


@dataclass(frozen=True, slots=True)
class StepResult:
    """The decision for this bar and the memory to pass into the next one."""

    state: RunnerState
    decision: BarDecision


def config_from_document(document: Mapping[str, Any]) -> RunnerConfig:
    """Read the decision fields from a validated IR document.

    This does not validate and does not copy expression trees. The caller
    keeps the document stable for the run.
    """
    actions = {str(action["id"]): action for action in document["actions"]}
    rules = tuple(document["rules"])
    return RunnerConfig(
        asset_ids=tuple(str(asset["id"]) for asset in document["assets"]),
        warmup_bars=int(document["engine"]["warmup_bars"]),
        signals=tuple((str(signal["id"]), signal["expr"]) for signal in document["signals"]),
        action_asset=tuple(
            (str(action["id"]), str(action["asset"])) for action in document["actions"]
        ),
        rules=rules,
        pairs=_pairs(rules, actions),
    )


def initial_state(asset_ids: Sequence[str]) -> RunnerState:
    """Empty cross and bars-since memory, one entry per asset, in that order."""
    empty = MemoryState()
    return RunnerState(tuple((asset_id, empty) for asset_id in asset_ids))


def make_window(
    t: int,
    open_time: int,
    series: Mapping[str, Mapping[str, Sequence[float]]],
    bars: Mapping[str, Bar | None],
) -> BarWindow:
    """A window that ends at ``t``. Longer series are not readable past ``t``."""
    if isinstance(t, bool) or not isinstance(t, int) or t < 0:
        raise BacktestError("bar index must be a whole number")
    if isinstance(open_time, bool) or not isinstance(open_time, int):
        raise BacktestError("bar open_time is not an integer second")
    end = t + 1
    closed: dict[str, dict[str, ClosedSeries]] = {}
    for asset_id, columns in series.items():
        closed[asset_id] = {name: ClosedSeries(values, end) for name, values in columns.items()}
    return BarWindow(t, open_time, closed, dict(bars))


def step(
    config: RunnerConfig,
    state: RunnerState,
    window: BarWindow,
    view: BookView,
) -> StepResult:
    """Decide bar ``window.t``. ``state`` and ``view`` are left as they were passed."""
    _reject_future(config, window)
    memory = _thaw(state)
    signal_map = dict(config.signals)
    signals: list[SignalTruth] = []
    for signal_id, asset_id in config.pairs:
        value = evaluate(
            signal_map[signal_id],
            EvalContext(
                window.t,
                window.series[asset_id],
                _entry(view, asset_id),
                memory[asset_id],
            ),
            (signal_id,),
        )
        signals.append(SignalTruth(signal_id, asset_id, value is True))
    truth = tuple(signals)
    if window.t < config.warmup_bars:
        decision = BarDecision(truth, (), ())
    else:
        decision = BarDecision(truth, _oco(config, view, window), _rules(config, truth, window))
    return StepResult(_freeze(config.asset_ids, memory), decision)


def run(
    config: RunnerConfig,
    state: RunnerState,
    windows: Sequence[BarWindow],
    views: Sequence[BookView],
) -> tuple[StepResult, ...]:
    """``step`` once per window, threading the returned state forward.

    ``views[i]`` is the book before ``windows[i]``, not a view this function
    computes. The same calls made one at a time return the same results.
    """
    if len(windows) != len(views):
        raise BacktestError("each bar window needs the book view from before that bar")
    current = state
    results: list[StepResult] = []
    for window, view in zip(windows, views, strict=True):
        outcome = step(config, current, window, view)
        results.append(outcome)
        current = outcome.state
    return tuple(results)


def _pairs(
    rules: Sequence[Mapping[str, Any]], actions: Mapping[str, Mapping[str, Any]]
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for rule in rules:
        pair = (str(rule["when"]), str(actions[str(rule["do"])]["asset"]))
        if pair not in pairs:
            pairs.append(pair)
    return tuple(pairs)


def _reject_future(config: RunnerConfig, window: BarWindow) -> None:
    """Bar ``t + 1`` is not in the window. A longer series is a look-ahead."""
    if window.t < 0:
        raise BacktestError("bar index must be a whole number")
    for asset_id in config.asset_ids:
        if asset_id not in window.bars or asset_id not in window.series:
            raise BacktestError(f"decision window is missing asset {asset_id}")
        columns = window.series[asset_id]
        if not columns:
            raise BacktestError(f"decision window has no series for asset {asset_id}")
        for name, values in columns.items():
            if not isinstance(values, ClosedSeries) or len(values) != window.t + 1:
                raise BacktestError(
                    f"series {name} on {asset_id} includes a bar after {window.t}"
                )


def _thaw(state: RunnerState) -> dict[str, EvalMemory]:
    thawed: dict[str, EvalMemory] = {}
    for asset_id, memory in state.memory:
        thawed[asset_id] = EvalMemory(dict(memory.cross_prev), dict(memory.since))
    return thawed


def _freeze(asset_ids: Sequence[str], memory: Mapping[str, EvalMemory]) -> RunnerState:
    frozen: list[tuple[str, MemoryState]] = []
    for asset_id in asset_ids:
        item = memory[asset_id]
        frozen.append(
            (
                asset_id,
                MemoryState(tuple(item.cross_prev.items()), tuple(item.since.items())),
            )
        )
    return RunnerState(tuple(frozen))


def _entry(view: BookView, asset_id: str) -> float | None:
    for name, entry in view.entries:
        if name == asset_id:
            return float(entry)
    return None


def _oco(
    config: RunnerConfig, view: BookView, window: BarWindow
) -> tuple[OcoDrop | OcoTouch, ...]:
    positioned = {asset_id for asset_id, _entry_price in view.entries}
    levels = dict(view.ocos)
    actions: list[OcoDrop | OcoTouch] = []
    for asset_id in config.asset_ids:
        oco = levels.get(asset_id)
        if oco is None or asset_id not in positioned:
            if oco is not None:
                actions.append(OcoDrop(asset_id))
            continue
        if oco.attached_at == window.open_time:
            continue
        bar = window.bars[asset_id]
        if bar is None or bar.synthetic:
            continue
        leg = _oco_leg(bar, oco)
        if leg is None:
            continue
        raw = _live_price(bar, oco, leg)
        below = leg == "take_profit" and raw < _price(bar.low)
        actions.append(OcoTouch(asset_id, leg, raw, below))
    return tuple(actions)


def _oco_leg(bar: Bar, oco: OcoLevels) -> str | None:
    """Stop wins when one bar touches both levels. A hole is not passed in."""
    if _price(bar.low) <= oco.stop_loss:
        return "stop_loss"
    if _price(bar.high) >= oco.take_profit:
        return "take_profit"
    return None


def _live_price(bar: Bar, oco: OcoLevels, leg: str) -> Decimal:
    """Limit at the target. Stop at the stop, or at the open when gapped through."""
    opened = _price(bar.open)
    if leg == "stop_loss" and opened <= oco.stop_loss:
        return opened
    if leg == "stop_loss":
        return oco.stop_loss
    return oco.take_profit


def _rules(
    config: RunnerConfig, signals: Sequence[SignalTruth], window: BarWindow
) -> tuple[RuleAttempt, ...]:
    truth = {(item.signal_id, item.asset_id): item.is_true for item in signals}
    assets = dict(config.action_asset)
    attempts: list[RuleAttempt] = []
    for rule in config.rules:
        action_id = str(rule["do"])
        asset_id = assets[action_id]
        if not truth[(str(rule["when"]), asset_id)]:
            continue
        bar = window.bars[asset_id]
        if bar is None:
            attempts.append(RuleAttempt(action_id, asset_id, "missing_bar", None))
            continue
        if bar.synthetic:
            attempts.append(RuleAttempt(action_id, asset_id, "synthetic_bar", None))
            continue
        attempts.append(RuleAttempt(action_id, asset_id, None, _price(bar.close)))
    return tuple(attempts)


def _price(value: float) -> Decimal:
    return Decimal(str(value))
