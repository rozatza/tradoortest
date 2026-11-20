"""CR kill-tests for PR #18 (ROZ-83). Each test names the mutant it kills.

Adopted from the review. The assertions are unchanged.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import app.backtest as backtest_pkg
from app.backtest.errors import BacktestError
from app.ohlcv.bars import Bar
from app.runner import (
    BarWindow,
    BookView,
    OcoDrop,
    OcoLevels,
    OcoTouch,
    RunnerConfig,
    initial_state,
    make_window,
    run,
    step,
)
from app.runner.core import ClosedSeries

_MINT = "SyntheticMint1111111111111111111111111111"
_T0 = 1_700_006_400


def _bar(i: int, close: float, *, high: float | None = None, low: float | None = None,
         open_: float | None = None, synthetic: bool = False) -> Bar:
    o = close if open_ is None else open_
    h = max(o, close) if high is None else high
    lo = min(o, close) if low is None else low
    return Bar(_MINT, "1m", _T0 + 60 * i, o, h, lo, close, 1.0, synthetic)


def _config(assets=("sol",), expr=None, warmup=0, rules=None) -> RunnerConfig:
    expr = expr or {
        "op": "gt",
        "left": {"op": "ref", "name": "close"},
        "right": {"op": "lit", "value": 0},
    }
    acts = tuple((f"enter_{a}", a) for a in assets)
    if rules is None:
        rules = tuple({"id": f"r_{a}", "when": "go", "do": f"enter_{a}"} for a in assets)
    pairs = tuple(dict.fromkeys(("go", dict(acts)[r["do"]]) for r in rules))
    return RunnerConfig(tuple(assets), warmup, (("go", expr),), acts, tuple(rules), pairs)


def _window(t: int, assets=("sol",), closes=None, bars=None):
    closes = closes or {a: [100.0] * (t + 1) for a in assets}
    bars = bars or {a: _bar(t, closes[a][t]) for a in assets}
    return make_window(t, _T0 + 60 * t, {a: {"close": closes[a]} for a in assets}, bars)


# --- ClosedSeries edges (look-ahead through slicing) ---------------------------

def test_negative_slice_start_cannot_read_the_buffer_tail() -> None:
    """Kills C7: a negative start must not wrap to the future end of the buffer."""
    s = ClosedSeries([1.0, 2.0, 3.0, 999.0, 998.0], 3)
    for bad in (lambda: s[-1:], lambda: s[-3:], lambda: s[:-1]):
        try:
            got = bad()
        except IndexError:
            continue
        assert 999.0 not in got and 998.0 not in got


def test_no_slice_reads_past_the_closed_end() -> None:
    """MUST-fix + kills C3: at head s[3:0:-1] on a window ending at 3 returns bar 3 (999)."""
    s = ClosedSeries([1.0, 2.0, 3.0, 999.0, 998.0], 3)
    bounds = [None, *range(-7, 8)]
    for start in bounds:
        for stop in bounds:
            for stride in (None, 1, 2, 3, -1, -2):
                try:
                    got = s[start:stop:stride]
                except IndexError:
                    continue
                assert 999.0 not in got and 998.0 not in got, (start, stop, stride, got)


def test_a_slice_that_does_not_raise_matches_the_closed_prefix() -> None:
    """At head s[::-1] and s[2::-1] are () instead of the reversed prefix."""
    prefix = [1.0, 2.0, 3.0]
    s = ClosedSeries([*prefix, 999.0], 3)
    bounds = [None, *range(-7, 8)]
    for start in bounds:
        for stop in bounds:
            for stride in (None, 1, 2, -1, -2):
                try:
                    got = s[start:stop:stride]
                except IndexError:
                    continue
                assert got == tuple(prefix[start:stop:stride]), (start, stop, stride, got)


def test_negative_index_is_from_the_closed_end() -> None:
    """Kills C1: s[-1] is bar t, not an error and not the buffer tail."""
    s = ClosedSeries([1.0, 2.0, 3.0, 999.0], 3)
    assert s[-1] == 3.0
    assert s[-3] == 1.0
    with pytest.raises(IndexError):
        s[-4]


def test_a_closed_series_cannot_claim_bars_the_buffer_lacks() -> None:
    """Kills C5."""
    with pytest.raises(BacktestError, match="missing a bar"):
        ClosedSeries([1.0, 2.0], 3)


# --- step guards -----------------------------------------------------------------

def test_a_raw_list_series_of_the_right_length_is_still_rejected() -> None:
    """Kills C8: only a ClosedSeries is a closed window."""
    bar = _bar(0, 100)
    window = BarWindow(0, bar.open_time, {"sol": {"close": [100.0]}}, {"sol": bar})
    with pytest.raises(BacktestError, match="after 0"):
        step(_config(), initial_state(("sol",)), window, BookView())


def test_a_window_missing_an_asset_is_a_backtest_error() -> None:
    """Kills C9: a missing asset is a BacktestError, not a KeyError."""
    config = _config(assets=("sol", "jup"))
    window = _window(0, assets=("sol",))
    with pytest.raises(BacktestError, match="missing asset jup"):
        step(config, initial_state(config.asset_ids), window, BookView())


def test_a_window_with_no_series_is_rejected() -> None:
    """Kills C10, including a literal-only signal that never reads a series."""
    config = _config(expr={"op": "lit", "value": True})
    bar = _bar(0, 100)
    window = make_window(0, bar.open_time, {"sol": {}}, {"sol": bar})
    with pytest.raises(BacktestError, match="no series"):
        step(config, initial_state(("sol",)), window, BookView())


def test_make_window_copies_the_bar_map() -> None:
    """Kills C13: a caller reusing its dict must not rewrite a stored window."""
    bars = {"sol": _bar(0, 100)}
    window = make_window(0, _T0, {"sol": {"close": [100.0]}}, bars)
    bars["sol"] = None
    assert window.bars["sol"] is not None


def test_run_with_unequal_views_is_a_backtest_error() -> None:
    """Kills C20."""
    with pytest.raises(BacktestError, match="book view"):
        run(_config(), initial_state(("sol",)), [_window(0)], [])


def test_a_numeric_signal_value_is_not_true() -> None:
    """Kills C16: only a boolean True is a true signal (same as the frozen engine)."""
    config = _config(expr={"op": "ref", "name": "close"})
    out = step(config, initial_state(("sol",)), _window(0), BookView())
    assert [s.is_true for s in out.decision.signals] == [False]
    assert out.decision.rules == ()


# --- OCO decisions -----------------------------------------------------------------

def test_warmup_never_settles_an_oco_even_with_a_held_book() -> None:
    """Kills C15: docs say decision.oco is empty during warmup (paper restart case)."""
    config = _config(warmup=5)
    bar = _bar(2, 100, high=130, low=80)
    window = _window(2, bars={"sol": bar})
    view = BookView(
        (("sol", Decimal("100")),),
        (("sol", OcoLevels(Decimal("120"), Decimal("90"), _T0)),),
    )
    out = step(config, initial_state(("sol",)), window, view)
    assert out.decision.oco == ()


def test_a_pair_whose_long_is_flat_is_dropped() -> None:
    """Kills C22: an OCO with no open long is an OcoDrop, not ignored."""
    view = BookView((), (("sol", OcoLevels(Decimal("120"), Decimal("90"), _T0)),))
    out = step(_config(), initial_state(("sol",)), _window(1), view)
    assert out.decision.oco == (OcoDrop("sol"),)


def test_oco_actions_follow_asset_order_not_book_order() -> None:
    """Kills C25: two touched pairs settle in IR asset order (fill order, cash, equity)."""
    assets = ("sol", "jup")
    config = _config(assets=assets)
    bars = {"sol": _bar(1, 100, high=101, low=80), "jup": _bar(1, 100, high=101, low=80)}
    window = _window(1, assets=assets, bars=bars)
    levels = OcoLevels(Decimal("150"), Decimal("90"), _T0)
    view = BookView(
        (("jup", Decimal("100")), ("sol", Decimal("100"))),
        (("jup", levels), ("sol", levels)),
    )
    out = step(config, initial_state(assets), window, view)
    assert out.decision.oco == (
        OcoTouch("sol", "stop_loss", Decimal("90"), False),
        OcoTouch("jup", "stop_loss", Decimal("90"), False),
    )


def test_entry_price_is_per_asset() -> None:
    """Kills C18: another asset's open long is not this asset's ENTRY_PRICE()."""
    assets = ("sol", "jup")
    expr = {
        "op": "gt",
        "left": {"op": "entry_price", "args": []},
        "right": {"op": "lit", "value": 0},
    }
    config = _config(assets=assets, expr=expr)
    view = BookView((("jup", Decimal("5")),), ())
    out = step(config, initial_state(assets), _window(1, assets=assets), view)
    truth = {(s.signal_id, s.asset_id): s.is_true for s in out.decision.signals}
    assert truth == {("go", "sol"): False, ("go", "jup"): True}


# --- lazy package -----------------------------------------------------------------

_BASE_ALL = [
    "METRIC_COLUMNS", "TRADE_COLUMNS", "BacktestError", "BacktestResult", "EquityPoint", "Fill",
    "Metrics", "OpenPosition", "SkippedOrder", "Trade", "metrics_csv", "metrics_json", "move_oco",
    "run_backtest", "trade_log_csv", "trade_log_json",
]


def test_public_names_are_the_base_names_and_star_import_binds_them() -> None:
    """Kills I5 (and any _EXPORTS gap): __all__ equals e56685c's and every name resolves."""
    assert list(backtest_pkg.__all__) == _BASE_ALL
    code = (
        "from app.backtest import *\n"
        f"names = {_BASE_ALL!r}\n"
        "missing = [n for n in names if globals().get(n) is None]\n"
        "raise SystemExit(1 if missing else 0)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_an_unknown_attribute_is_an_attribute_error() -> None:
    """Kills I4: hasattr must stay False for a name the package does not export."""
    assert not hasattr(backtest_pkg, "definitely_not_exported")
    with pytest.raises(AttributeError, match="definitely_not_exported"):
        backtest_pkg.definitely_not_exported  # noqa: B018
