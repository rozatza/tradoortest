"""Backtest engine: cents, warmup, holes, synthetic bars, and no look-ahead.

The hand-calc fixture is synthetic. Nothing here calls the network.
"""

from __future__ import annotations

import json
import time
from decimal import ROUND_DOWN, Decimal, Inexact, getcontext, localcontext
from fractions import Fraction
from pathlib import Path

import pytest

from app.backtest import BacktestError, move_oco, run_backtest, trade_log_csv, trade_log_json
from app.backtest.engine import _between, _flag_prefix
from app.backtest.money import decimal_ratio, floor_qty
from app.ohlcv.bars import Bar

_START = 1_767_225_600  # 2026-01-01T00:00:00Z
_MINT = "SyntheticMint111111111111111111111111111"
_FIXTURE = Path(__file__).parent / "fixtures" / "backtest" / "hand_round_trip.json"
_OCO_FIXTURE = Path(__file__).parent / "fixtures" / "backtest" / "hand_oco.json"

# Hand arithmetic for the fixture. Half-even cents. See the test docstring.
_BUY_PRICE = Decimal("100.05")
_BUY_NOTIONAL = Decimal("4995.00")
_BUY_COMMISSION = Decimal("5.00")
_EQUITY_AFTER_BUY = Decimal("9992.50")
_SELL_PRICE = Decimal("129.935")
_SELL_NOTIONAL = Decimal("6487.01")
_SELL_COMMISSION = Decimal("6.49")
_FINAL_EQUITY = Decimal("11480.52")


def _bar(
    offset: int,
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1.0,
    synthetic: bool = False,
    timeframe: str = "1m",
    mint: str = _MINT,
) -> Bar:
    if synthetic:
        return Bar(mint, timeframe, _START + offset, close, close, close, close, 0.0, True)
    opened = close if open_ is None else open_
    return Bar(
        mint,
        timeframe,
        _START + offset,
        opened,
        close if high is None else high,
        close if low is None else low,
        close,
        volume,
        False,
    )


def _risk(take_profit: float = 6, stop_loss: float = 3) -> dict:
    return {
        "type": "oco",
        "evaluation": "on_entry_fill",
        "movement": "tighten_only",
        "take_profit": {"mode": "percent", "percent": take_profit},
        "stop_loss": {"mode": "percent", "percent": stop_loss},
    }


def _strategy(**overrides: object) -> dict:
    document: dict = {
        "ir_version": "1.0",
        "strategy": {"id": "hand", "name": "Hand"},
        "engine": {
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
        "capital": {"initial": 10000, "currency": "USDC"},
        "costs": {"commission_bps": 0, "slippage_bps": 0},
        "assets": [
            {
                "id": "sol",
                "symbol": "SOL",
                "chain": "solana",
                "market": "spot",
                "quote": "USDC",
            }
        ],
        "derived_series": [],
        "signals": [{"id": "go", "expr": {"op": "lit", "value": True}}],
        "actions": [
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(),
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        "rules": [{"id": "enter_rule", "when": "go", "do": "enter_sol"}],
        "portfolio": {
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 1,
            "allow_pyramiding": False,
            "quote_currency": "USDC",
        },
    }
    for key, value in overrides.items():
        document[key] = value
    return document


def _gt(name: str, level: int | float) -> dict:
    return {
        "op": "gt",
        "left": {"op": "ref", "name": name},
        "right": {"op": "lit", "value": level},
    }


def _eq(name: str, level: int | float) -> dict:
    return {
        "op": "eq",
        "left": {"op": "ref", "name": name},
        "right": {"op": "lit", "value": level},
    }


def test_hand_fixture_is_labeled_synthetic() -> None:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    text = _FIXTURE.read_text(encoding="utf-8").lower()
    assert payload["synthetic"] is True
    assert "not market data" in payload["note"].lower()
    for forbidden in ("api-key", "authorization", "bearer", "x-cg-", "account_id"):
        assert forbidden not in text
    expected = payload["expected"]
    assert Decimal(expected["buy_price"]) == _BUY_PRICE
    assert Decimal(expected["buy_notional"]) == _BUY_NOTIONAL
    assert Decimal(expected["buy_commission"]) == _BUY_COMMISSION
    assert Decimal(expected["equity_after_buy"]) == _EQUITY_AFTER_BUY
    assert Decimal(expected["sell_price"]) == _SELL_PRICE
    assert Decimal(expected["sell_notional"]) == _SELL_NOTIONAL
    assert Decimal(expected["sell_commission"]) == _SELL_COMMISSION
    assert Decimal(expected["final_equity"]) == _FINAL_EQUITY


def test_hand_computed_round_trip_matches_to_the_cent() -> None:
    """One round trip. The book is half-even USDC cents.

    Capital 10000.00. Commission 10 bps. Slippage 5 bps. Enter 50% of equity
    on the second bar and exit all of the long on the third. The first bar
    is warmup, so it does not trade. The entry's OCO is 100% / 99%, wider
    than this bar's 90–140 range, so the signal sells and the pair does not.

    Buy close 100. Fill = 100 * 10005/10000 = 100.05.
    Budget = 50% of 10000.00 = 5000.00.
    Room = round_half_even(5000 * 10000/10010) = round_half_even(4995.004995...) = 4995.00.
    Quantity = floor(4995.00 / 100.05) to 1e-12.
    Notional = round_half_even(quantity * 100.05) = 4995.00.
    Commission = round_half_even(4995.00 * 10/10000) = round_half_even(4.995) = 5.00.
    Cash left = 5000.00.
    Equity marked at close 100 = round_half_even(5000 + quantity * 100) = 9992.50.

    Sell close 130. Fill = 130 * 9995/10000 = 129.935.
    Notional = round_half_even(quantity * 129.935) = round_half_even(6487.009745...) = 6487.01.
    Commission = round_half_even(6487.01 * 10/10000) = round_half_even(6.48701) = 6.49.
    Cash = 5000.00 + 6487.01 - 6.49 = 11480.52.
    """
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    bars = [
        Bar(
            _MINT,
            "1m",
            _START + item["offset"],
            item["open"],
            item["high"],
            item["low"],
            item["close"],
            item["volume"],
            False,
        )
        for item in payload["bars"]
    ]
    document = _strategy(
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 1,
        },
        costs={"commission_bps": 10, "slippage_bps": 5},
        signals=[
            {"id": "go", "expr": _gt("close", 50)},
            {"id": "stop", "expr": _gt("close", 120)},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    first = run_backtest(document, {"sol": bars})
    second = run_backtest(document, {"sol": bars})
    assert first == second
    assert [fill.open_time for fill in first.fills] == [_START + 60, _START + 120]
    buy, sell = first.fills
    assert buy.side == "buy"
    assert buy.price == _BUY_PRICE
    assert buy.notional == _BUY_NOTIONAL
    assert buy.commission == _BUY_COMMISSION
    # floor(4995 / 100.05 * 1e12) / 1e12, from integer division. This remainder
    # is below one half, so the case that separates floor from half-even is
    # the 5000 / 3 buy below.
    assert buy.qty == Decimal("49.925037481259")
    assert buy.equity_after == _EQUITY_AFTER_BUY
    assert sell.side == "sell"
    assert sell.price == _SELL_PRICE
    assert sell.notional == _SELL_NOTIONAL
    assert sell.commission == _SELL_COMMISSION
    assert sell.qty == buy.qty
    assert first.final_cash == _FINAL_EQUITY
    assert first.final_equity == _FINAL_EQUITY
    assert first.final_positions == ()
    assert first.equity_curve[0].equity == Decimal("10000.00")
    assert first.equity_curve[1].equity == _EQUITY_AFTER_BUY
    assert first.equity_curve[2].equity == _FINAL_EQUITY
    assert all(point.cash >= 0 for point in first.equity_curve)


def test_warmup_and_indicator_warmup_block_signals() -> None:
    document = _strategy(
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 2,
        },
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    bars = [_bar(0, 10), _bar(60, 10), _bar(120, 10)]
    result = run_backtest(document, {"sol": bars})
    assert [fill.open_time for fill in result.fills] == [_START + 120]

    sma_doc = _strategy(
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
        derived_series=[
            {
                "id": "sma_3",
                "kind": "indicator",
                "indicator": "sma",
                "source": "close",
                "params": {"period": 3},
            }
        ],
        signals=[
            {
                "id": "go",
                "expr": {
                    "op": "gt",
                    "left": {"op": "ref", "name": "close"},
                    "right": {"op": "ref", "name": "sma_3"},
                },
            }
        ],
    )
    # Mean of 1, 1, 5 is about 2.33. Only the third bar has a defined SMA, and 5 is above it.
    warmed = run_backtest(sma_doc, {"sol": [_bar(0, 1), _bar(60, 1), _bar(120, 5)]})
    assert [fill.open_time for fill in warmed.fills] == [_START + 120]


def test_missing_bars_are_not_filled_forward() -> None:
    # Closes 10, hole, 12. SMA(2) on the bar after the hole includes NaN, so
    # close > sma is false. Compressing the hole to [10, 12] would make SMA 11
    # and the second bar would buy. A copied close of 10 in the hole would too.
    document = _strategy(
        derived_series=[
            {
                "id": "sma_2",
                "kind": "indicator",
                "indicator": "sma",
                "source": "close",
                "params": {"period": 2},
            }
        ],
        signals=[
            {
                "id": "go",
                "expr": {
                    "op": "gt",
                    "left": {"op": "ref", "name": "close"},
                    "right": {"op": "ref", "name": "sma_2"},
                },
            }
        ],
    )
    bars = [_bar(0, 10), _bar(120, 12)]
    result = run_backtest(document, {"sol": bars})
    assert result.fills == ()
    assert [point.open_time for point in result.equity_curve] == [_START, _START + 60, _START + 120]
    assert all(point.equity == Decimal("10000.00") for point in result.equity_curve)


def test_a_hole_does_not_complete_a_cross() -> None:
    document = _strategy(
        signals=[
            {
                "id": "go",
                "expr": {
                    "op": "cross_above",
                    "args": [
                        {"op": "ref", "name": "close"},
                        {"op": "lit", "value": 2},
                    ],
                },
            }
        ],
    )
    # Without the hole, 1 then 3 crosses above 2. The hole clears the previous sample.
    result = run_backtest(document, {"sol": [_bar(0, 1), _bar(120, 3)]})
    assert result.fills == ()
    intact = run_backtest(document, {"sol": [_bar(0, 1), _bar(60, 3)]})
    assert [fill.open_time for fill in intact.fills] == [_START + 60]


def test_synthetic_bars_do_not_fill_and_are_not_queued() -> None:
    document = _strategy(signals=[{"id": "go", "expr": _gt("close", 100)}])
    bars = [
        _bar(0, 200, synthetic=True),
        _bar(60, 40),
    ]
    result = run_backtest(document, {"sol": bars})
    assert result.fills == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "synthetic_bar"
    assert result.skipped[0].open_time == _START
    assert result.final_positions == ()

    real_zero_volume = run_backtest(document, {"sol": [_bar(0, 200, volume=0.0)]})
    assert len(real_zero_volume.fills) == 1
    assert real_zero_volume.fills[0].price == Decimal("200.0")


def test_poisoned_future_bar_does_not_change_earlier_fills() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 5},
        signals=[{"id": "go", "expr": _gt("close", 150)}],
    )
    bars = [_bar(0, 100), _bar(60, 200), _bar(120, 180)]
    original = run_backtest(document, {"sol": bars})
    poisoned = [_bar(0, 100), _bar(60, 200), _bar(120, 1)]
    mutated = run_backtest(document, {"sol": poisoned})
    assert original.fills[0].open_time == _START + 60
    assert original.fills[0].price == Decimal("200") * Decimal("10005") / Decimal("10000")
    assert [fill for fill in original.fills if fill.open_time < _START + 120] == [
        fill for fill in mutated.fills if fill.open_time < _START + 120
    ]
    assert [point for point in original.equity_curve if point.open_time < _START + 120] == [
        point for point in mutated.equity_curve if point.open_time < _START + 120
    ]
    # A signal that peeked at the poisoned close of 1 would not buy the 200 bar
    # if it used that close, and a fill that used it would not print 201.
    assert mutated.fills[0].price == Decimal("200.1")


def test_dropping_the_future_does_not_change_earlier_fills() -> None:
    document = _strategy(signals=[{"id": "go", "expr": _gt("close", 150)}])
    full = [_bar(0, 100), _bar(60, 200), _bar(120, 900)]
    short = full[:-1]
    full_result = run_backtest(document, {"sol": full})
    short_result = run_backtest(document, {"sol": short})
    assert short_result.fills == tuple(
        fill for fill in full_result.fills if fill.open_time < _START + 120
    )
    assert short_result.equity_curve == tuple(
        point for point in full_result.equity_curve if point.open_time < _START + 120
    )


def test_a_future_close_cannot_turn_the_current_signal_on() -> None:
    # Bar 1's own close is 100, which is not above 150. Bar 2's close is.
    # Peeking one bar ahead would buy on bar 1.
    document = _strategy(signals=[{"id": "go", "expr": _gt("close", 150)}])
    honest = run_backtest(
        document, {"sol": [_bar(0, 80), _bar(60, 100), _bar(120, 5000)]}
    )
    peeked_price = run_backtest(
        document, {"sol": [_bar(0, 80), _bar(60, 100), _bar(120, 100)]}
    )
    assert [fill.open_time for fill in honest.fills] == [_START + 120]
    assert [fill.open_time for fill in peeked_price.fills if fill.open_time < _START + 120] == []
    assert honest.fills[0].price == Decimal("5000.0")


def test_long_only_spot_does_not_short_or_spend_more_than_cash() -> None:
    exit_only = _strategy(
        signals=[
            {"id": "go", "expr": _gt("close", 1000)},
            {"id": "stop", "expr": {"op": "lit", "value": True}},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    flat = run_backtest(exit_only, {"sol": [_bar(0, 100)]})
    assert flat.fills == ()
    assert flat.skipped[0].reason == "no_position"
    assert flat.final_cash == Decimal("10000.00")

    spend_all = _strategy(
        costs={"commission_bps": 100, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    filled = run_backtest(spend_all, {"sol": [_bar(0, 100)]})
    assert filled.fills[0].notional == Decimal("9900.99")
    assert filled.fills[0].commission == Decimal("99.01")
    assert filled.final_cash == Decimal("0.00")
    assert filled.final_positions[0].qty > 0

    again = _strategy(signals=[{"id": "go", "expr": {"op": "lit", "value": True}}])
    pyramided = run_backtest(again, {"sol": [_bar(0, 100), _bar(60, 100)]})
    assert len(pyramided.fills) == 1
    assert pyramided.skipped[0].reason == "pyramiding"


def test_stop_loss_exits_when_there_is_no_exit_signal() -> None:
    # Stop is 3% under the fill of 100, so 97. The next bar trades down to 50
    # and opens at 100, above the stop, so the fill is the stop. No signal exit.
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _gt("close", 80)},
            {"id": "stop", "expr": _gt("close", 1000)},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    result = run_backtest(
        document,
        {"sol": [_bar(0, 100, low=100), _bar(60, 50, open_=100, high=100, low=50)]},
    )
    assert [fill.leg for fill in result.fills] == ["market", "stop_loss"]
    assert result.fills[1].price == Decimal("97.0")
    assert result.fills[1].notional == Decimal("9700.00")
    assert result.final_positions == ()
    assert result.final_cash == Decimal("9700.00")


def test_percent_of_position_sells_part_of_the_long() -> None:
    document = _strategy(
        signals=[
            {"id": "go", "expr": {"op": "lit", "value": True}},
            {"id": "stop", "expr": {"op": "lit", "value": True}},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(),
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 50},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 1,
        },
    )
    # Both signals are true on bar 0, but warmup blocks it. Bar 1 snapshots
    # entry_price before the fill, so the exit is still true from the literal
    # and runs after the entry in rule order... warmup 1 means bar 0 does not
    # trade. Bar 1: both literals are true before the fill, so enter then exit
    # 50% on the same bar.
    result = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 100)]})
    assert [fill.side for fill in result.fills] == ["buy", "sell"]
    assert result.fills[1].qty * 2 == result.fills[0].qty
    assert result.final_positions[0].qty == result.fills[0].qty - result.fills[1].qty
    assert result.final_cash == Decimal("5000.00")


def test_max_positions_blocks_a_second_asset() -> None:
    document = _strategy(
        assets=[
            {
                "id": "sol",
                "symbol": "SOL",
                "chain": "solana",
                "market": "spot",
                "quote": "USDC",
            },
            {
                "id": "jup",
                "symbol": "JUP",
                "chain": "solana",
                "market": "spot",
                "quote": "USDC",
            },
        ],
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 25},
                "risk": _risk(),
            },
            {
                "id": "enter_jup",
                "type": "enter",
                "side": "long",
                "asset": "jup",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 25},
                "risk": _risk(),
            },
        ],
        rules=[
            {"id": "enter_sol_rule", "when": "go", "do": "enter_sol"},
            {"id": "enter_jup_rule", "when": "go", "do": "enter_jup"},
        ],
    )
    result = run_backtest(
        document,
        {"sol": [_bar(0, 100)], "jup": [_bar(0, 2, mint="OtherMint")]},
    )
    assert [fill.asset_id for fill in result.fills] == ["sol"]
    assert result.skipped[0].reason == "max_positions"
    assert result.skipped[0].asset_id == "jup"


def test_entry_price_is_empty_until_the_fill_and_then_the_average() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _gt("close", 50)},
            {
                "id": "stop",
                "expr": {
                    "op": "gt",
                    "left": {"op": "entry_price", "args": []},
                    "right": {"op": "lit", "value": 50},
                },
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    result = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 100)]})
    assert [fill.side for fill in result.fills] == ["buy", "sell"]
    assert result.fills[0].open_time == _START
    assert result.fills[1].open_time == _START + 60
    assert result.final_positions == ()


def test_invalid_ir_and_bad_bars_are_refused() -> None:
    broken = _strategy()
    del broken["ir_version"]
    with pytest.raises(BacktestError):
        run_backtest(broken, {"sol": [_bar(0, 1)]})
    with pytest.raises(BacktestError, match="unknown asset"):
        run_backtest(_strategy(), {"sol": [_bar(0, 1)], "nope": [_bar(0, 1)]})
    with pytest.raises(BacktestError, match="no bars"):
        run_backtest(_strategy(), {})
    with pytest.raises(BacktestError, match="no bars for asset sol"):
        run_backtest(_strategy(), {"sol": []})


def test_a_shifted_indicator_would_fill_early() -> None:
    # SMA(2) of 1, 1, 1, 100 is nan, 1, 1, 50.5. Only the last bar is above 10.
    # Reading the series one bar ahead would buy the third bar at close 1.
    document = _strategy(
        derived_series=[
            {
                "id": "sma_2",
                "kind": "indicator",
                "indicator": "sma",
                "source": "close",
                "params": {"period": 2},
            }
        ],
        signals=[{"id": "go", "expr": _gt("sma_2", 10)}],
    )
    result = run_backtest(
        document,
        {"sol": [_bar(0, 1), _bar(60, 1), _bar(120, 1), _bar(180, 100)]},
    )
    assert [fill.open_time for fill in result.fills] == [_START + 180]
    assert result.fills[0].price == Decimal("100")


def test_a_signal_on_a_missing_bar_is_skipped() -> None:
    # Enter on the first bar. The exit is true only on the hole (bars_since
    # counts it). Filling that signal at the prior close would sell there.
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _gt("close", 150)},
            {
                "id": "stop",
                "expr": {
                    "op": "gt",
                    "left": {
                        "op": "bars_since",
                        "args": [_gt("close", 0)],
                    },
                    "right": {"op": "lit", "value": 0},
                },
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 200), _bar(120, 100)]})
    assert [fill.open_time for fill in result.fills] == [_START]
    assert result.fills[0].side == "buy"
    assert result.fills[0].price == Decimal("200")
    assert [(item.open_time, item.reason) for item in result.skipped] == [
        (_START + 60, "missing_bar")
    ]
    assert len(result.final_positions) == 1


def test_a_buy_cannot_spend_more_cash_than_the_book_has() -> None:
    document = _strategy(
        assets=[
            {
                "id": "sol",
                "symbol": "SOL",
                "chain": "solana",
                "market": "spot",
                "quote": "USDC",
            },
            {
                "id": "jup",
                "symbol": "JUP",
                "chain": "solana",
                "market": "spot",
                "quote": "USDC",
            },
        ],
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 2,
            "allow_pyramiding": False,
            "quote_currency": "USDC",
        },
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(),
            },
            {
                "id": "enter_jup",
                "type": "enter",
                "side": "long",
                "asset": "jup",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(),
            },
        ],
        rules=[
            {"id": "enter_sol_rule", "when": "go", "do": "enter_sol"},
            {"id": "enter_jup_rule", "when": "go", "do": "enter_jup"},
        ],
    )
    result = run_backtest(
        document,
        {"sol": [_bar(0, 100)], "jup": [_bar(0, 2, mint="OtherMint")]},
    )
    assert [fill.asset_id for fill in result.fills] == ["sol"]
    assert result.skipped[0].reason == "no_cash"
    assert result.final_cash == Decimal("0.00")
    assert all(point.cash >= 0 for point in result.equity_curve)


def test_sparse_sma_is_linted_and_an_empty_asset_is_an_error() -> None:
    document = _strategy(
        engine={
            "bar_size": "5m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
        derived_series=[
            {
                "id": "sma_20",
                "kind": "indicator",
                "indicator": "sma",
                "source": "close",
                "params": {"period": 20},
            }
        ],
        signals=[{"id": "go", "expr": _gt("sma_20", 0)}],
    )
    result = run_backtest(
        document,
        {"sol": [_bar(0, 10, timeframe="5m"), _bar(39 * 300, 10, timeframe="5m")]},
    )
    assert result.fills == ()
    assert result.lints == ("derived series sma_20 on sol is defined on 0 of 40 bars",)


def test_shared_subexpression_matches_a_copied_one() -> None:
    cross = {
        "op": "cross_above",
        "args": [{"op": "ref", "name": "close"}, {"op": "lit", "value": 5}],
    }
    shared = {"op": "and", "left": cross, "right": cross}
    copied = json.loads(json.dumps(shared))
    bars = [_bar(0, 1), _bar(60, 1), _bar(120, 10)]

    def run(expr: dict) -> list[int]:
        document = _strategy(signals=[{"id": "go", "expr": expr}])
        return [fill.open_time for fill in run_backtest(document, {"sol": bars}).fills]

    assert run(shared) == run(copied) == [_START + 120]


def test_extreme_prices_price_to_the_cent() -> None:
    """A close beyond the take-profit exits at the limit, not at that close.

    These rows used to mark the open long at the far price. The 6% target
    now sells at entry × 1.06. That product still has to quantize, and the
    far close must not become the fill. A synthetic second bar is not an
    OCO touch, so the same rows still mark the open long at the far close.
    """

    def exited(capital: float, buy: float, mark: float) -> Decimal:
        document = _strategy(
            capital={"initial": capital, "currency": "USDC"},
            costs={"commission_bps": 0, "slippage_bps": 0},
            signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        )
        result = run_backtest(
            document,
            {"sol": [_bar(0, buy), _bar(60, mark)]},
        )
        assert result.fills[1].leg == "take_profit"
        assert result.fills[1].price == Decimal(str(buy)) * Decimal("1.06")
        assert result.fills[1].price != Decimal(str(mark))
        assert all(point.cash >= 0 for point in result.equity_curve)
        return result.final_equity

    def marked(capital: float, buy: float, mark: float) -> Decimal:
        document = _strategy(
            capital={"initial": capital, "currency": "USDC"},
            costs={"commission_bps": 0, "slippage_bps": 0},
            signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        )
        result = run_backtest(
            document,
            {"sol": [_bar(0, buy), _bar(60, mark, synthetic=True)]},
        )
        assert [fill.leg for fill in result.fills] == ["market"]
        assert result.final_cash == Decimal("0.00")
        assert all(point.cash >= 0 for point in result.equity_curve)
        return result.final_equity

    assert exited(1e12, 1e-5, 1e9) == Decimal("1060000000000.00")
    assert exited(1e12, 0.01, 1e15) == Decimal("1060000000000.00")
    assert exited(10000, 1e-9, 1e15) == Decimal("10600.00")
    assert exited(10000, 2e-5, 1e9) == Decimal("10600.00")
    assert marked(1e12, 1e-5, 1e9) == Decimal("1E+26")
    assert marked(1e12, 0.01, 1e15) == Decimal("1E+29")
    assert marked(10000, 1e-9, 1e15) == Decimal("1E+28")
    assert marked(10000, 2e-5, 1e9) == Decimal("5E+17")
    # The open long is still marked at the far close. Serializing that cent in
    # the default 28-digit context used to raise InvalidOperation.
    for capital, buy, mark in ((1e12, 0.01, 1e15), (10000, 1e-9, 1e15)):
        document = _strategy(
            capital={"initial": capital, "currency": "USDC"},
            costs={"commission_bps": 0, "slippage_bps": 0},
            signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        )
        with localcontext() as ctx:
            ctx.prec = 28
            result = run_backtest(
                document,
                {"sol": [_bar(0, buy), _bar(60, mark, synthetic=True)]},
            )
            csv_text = trade_log_csv(result.trades)
            payload = json.loads(trade_log_json(result.trades))
        assert "NaN" not in csv_text
        assert payload["trades"][0]["unrealized_pnl"]
        assert "E" not in payload["trades"][0]["unrealized_pnl"]
        assert "E" not in payload["trades"][0]["entry_price"]


def test_sizing_edges_and_a_zero_percent_message() -> None:
    zero = _strategy()
    zero["actions"][0]["sizing"]["percent"] = 0
    with pytest.raises(BacktestError, match=r"greater than 0 and at most 100"):
        run_backtest(zero, {"sol": [_bar(0, 1)]})

    tiny = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 0.01},
                "risk": _risk(),
            }
        ],
    )
    bought = run_backtest(tiny, {"sol": [_bar(0, 1)]})
    assert bought.fills[0].notional == Decimal("1.00")
    assert bought.fills[0].commission == Decimal("0.00")
    assert bought.final_cash == Decimal("9999.00")

    full = _strategy(
        costs={"commission_bps": 10000, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": {"op": "lit", "value": True}},
            {"id": "stop", "expr": {"op": "lit", "value": True}},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 1,
        },
    )
    # Warmup blocks bar 0. Bar 1 enters 100% and then exits, because both
    # signals were already true. Commission of 10000 bps takes the whole
    # notional on the way in and again on the way out, so the account ends at 0.
    closed = run_backtest(full, {"sol": [_bar(0, 100), _bar(60, 90)]})
    assert [(fill.notional, fill.commission) for fill in closed.fills] == [
        (Decimal("5000.00"), Decimal("5000.00")),
        (Decimal("5000.00"), Decimal("5000.00")),
    ]
    assert closed.final_equity == Decimal("0.00")
    assert closed.final_cash == Decimal("0.00")

    with pytest.raises(BacktestError, match="less than 10000"):
        run_backtest(
            _strategy(costs={"commission_bps": 0, "slippage_bps": 10000}),
            {"sol": [_bar(0, 100)]},
        )

    # 100% equity, no costs: buy 10000, sell at 90 for 9000, buy 9000 again.
    # Extra enters while cash is 0 are no_cash, not a negative balance.
    round_trip = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 1,
            "allow_pyramiding": True,
            "quote_currency": "USDC",
        },
        signals=[
            {"id": "go", "expr": _gt("close", 95)},
            {
                "id": "stop",
                "expr": {
                    "op": "lt",
                    "left": {"op": "ref", "name": "close"},
                    "right": {"op": "lit", "value": 95},
                },
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    round_trip["actions"][0]["risk"] = _risk(100, 99)
    cycled = run_backtest(
        round_trip,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 100),
                _bar(120, 90),
                _bar(180, 100),
                _bar(240, 100),
            ]
        },
    )
    assert [(fill.side, fill.notional, fill.commission) for fill in cycled.fills] == [
        ("buy", Decimal("10000.00"), Decimal("0.00")),
        ("sell", Decimal("9000.00"), Decimal("0.00")),
        ("buy", Decimal("9000.00"), Decimal("0.00")),
    ]
    assert [item.reason for item in cycled.skipped] == ["no_cash", "no_cash"]
    assert cycled.final_equity == Decimal("9000.00")
    assert min(point.cash for point in cycled.equity_curve) == Decimal("0.00")

    heavy = _strategy(
        costs={"commission_bps": 9999, "slippage_bps": 9999},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    paid = run_backtest(heavy, {"sol": [_bar(0, 1)]})
    assert paid.fills[0].notional == Decimal("5000.25")
    assert paid.fills[0].commission == Decimal("4999.75")
    assert paid.final_cash == Decimal("0.00")


def test_cents_round_half_even_on_a_tie() -> None:
    # 50 * 1.0001 = 50.005. Half-even keeps 50.00. Half-up would be 50.01.
    document = _strategy(
        capital={"initial": 100, "currency": "USDC"},
        signals=[
            {"id": "go", "expr": _gt("close", 1.5)},
            {
                "id": "stop",
                "expr": {
                    "op": "lt",
                    "left": {"op": "ref", "name": "close"},
                    "right": {"op": "lit", "value": 1.5},
                },
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    result = run_backtest(document, {"sol": [_bar(0, 2.0), _bar(60, 1.0001)]})
    assert result.fills[0].notional == Decimal("100.00")
    assert result.fills[1].notional == Decimal("50.00")
    assert result.final_cash == Decimal("50.00")


def test_a_hole_is_marked_at_the_prior_close() -> None:
    document = _strategy(costs={"commission_bps": 0, "slippage_bps": 0})
    # 100% / 99% keeps the long open at 150. A 6% target would have sold it.
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 100), _bar(120, 150)]})
    hole = next(point for point in result.equity_curve if point.open_time == _START + 60)
    later = next(point for point in result.equity_curve if point.open_time == _START + 120)
    assert hole.equity == Decimal("10000.00")
    assert later.equity == Decimal("15000.00")
    assert result.final_positions[0].qty == Decimal("100")


def test_a_pyramid_sizes_from_equity_not_cash() -> None:
    enter = {
        "id": "enter_sol",
        "type": "enter",
        "side": "long",
        "asset": "sol",
        "order": {"type": "market"},
        "sizing": {"mode": "percent_equity", "percent": 50},
        "risk": _risk(),
    }
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        actions=[
            enter,
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 1,
            "allow_pyramiding": True,
            "quote_currency": "USDC",
        },
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 50)]})
    assert [fill.notional for fill in result.fills] == [Decimal("5000.00"), Decimal("3750.00")]


def test_decimal_context_stays_local() -> None:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    bars = [
        Bar(
            _MINT,
            "1m",
            _START + item["offset"],
            item["open"],
            item["high"],
            item["low"],
            item["close"],
            item["volume"],
            False,
        )
        for item in payload["bars"]
    ]
    document = _strategy(
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 1,
        },
        costs={"commission_bps": 10, "slippage_bps": 5},
        signals=[
            {"id": "go", "expr": _gt("close", 50)},
            {"id": "stop", "expr": _gt("close", 120)},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    ctx = getcontext()
    saved = (ctx.prec, ctx.rounding)
    ctx.prec = 6
    ctx.rounding = ROUND_DOWN
    try:
        result = run_backtest(document, {"sol": bars})
        assert result.final_equity == _FINAL_EQUITY
        assert getcontext().prec == 6
        assert getcontext().rounding == ROUND_DOWN
    finally:
        ctx.prec, ctx.rounding = saved


def test_a_series_defined_on_exactly_half_the_bars_is_not_linted() -> None:
    document = _strategy(
        derived_series=[
            {
                "id": "sma_3",
                "kind": "indicator",
                "indicator": "sma",
                "source": "close",
                "params": {"period": 3},
            }
        ]
    )
    result = run_backtest(document, {"sol": [_bar(offset, 1.0) for offset in range(0, 240, 60)]})
    assert result.lints == ()


def test_an_indicator_warning_is_a_result_lint() -> None:
    document = _strategy(
        derived_series=[
            {
                "id": "stoch_k",
                "kind": "indicator",
                "indicator": "stochastic",
                "source": "close",
                "output": "k",
                "params": {"k_period": 1, "d_period": 1},
            }
        ]
    )
    result = run_backtest(document, {"sol": [_bar(0, 1.0), _bar(60, 1.0)]})
    assert any(
        note.startswith("stoch_k on sol: ") and "highest high equals the lowest low" in note
        for note in result.lints
    )


def _oco_hand() -> tuple[dict, dict]:
    payload = json.loads(_OCO_FIXTURE.read_text(encoding="utf-8"))
    text = _OCO_FIXTURE.read_text(encoding="utf-8").lower()
    assert payload["synthetic"] is True
    assert "not market data" in payload["note"].lower()
    for forbidden in ("api-key", "authorization", "bearer", "x-cg-", "account_id"):
        assert forbidden not in text
    document = _strategy(
        costs=payload["costs"],
        signals=[{"id": "go", "expr": _eq("close", 100)}],
    )
    document["actions"][0]["risk"] = _risk(
        payload["take_profit_percent"], payload["stop_loss_percent"]
    )
    return payload, document


def _fixture_bar(item: dict, *, synthetic: bool = False) -> Bar:
    if synthetic:
        return _bar(item["offset"], item["close"], synthetic=True)
    return _bar(
        item["offset"],
        item["close"],
        open_=item["open"],
        high=item["high"],
        low=item["low"],
        volume=item["volume"],
    )


def _money(payload: dict, name: str) -> Decimal:
    return Decimal(payload["expected"][name])


def test_hand_oco_fixture_is_labeled_synthetic() -> None:
    payload, _document = _oco_hand()
    assert payload["expected"]["take_profit_cash"] == "10972.54"
    assert payload["expected"]["stop_loss_cash"] == "9476.27"


def test_take_profit_fills_at_the_limit_and_ignores_the_entry_wick() -> None:
    """Entry wick trades 1–200. It is not read. The next bar hits 10% only.

    Buy fill = 100.05. Notional = 9990.01. Commission = 9.99.
    Take-profit = 110.055. Sell fill = 109.9999725.
    Notional = 10983.52. Commission = 10.98. Cash = 10972.54.
    """
    payload, document = _oco_hand()
    alone = run_backtest(document, {"sol": [_fixture_bar(payload["entry"])]})
    assert [fill.leg for fill in alone.fills] == ["market"]
    assert len(alone.final_positions) == 1

    result = run_backtest(
        document,
        {"sol": [_fixture_bar(payload["entry"]), _fixture_bar(payload["take_profit_bar"])]},
    )
    assert [fill.leg for fill in result.fills] == ["market", "take_profit"]
    buy, sell = result.fills
    assert buy.open_time == _START
    assert sell.open_time == _START + 60
    assert buy.price == _money(payload, "buy_price")
    assert buy.notional == _money(payload, "buy_notional")
    assert buy.commission == _money(payload, "buy_commission")
    assert sell.price == _money(payload, "take_profit_fill")
    assert sell.notional == _money(payload, "take_profit_notional")
    assert sell.commission == _money(payload, "take_profit_commission")
    assert sell.qty == buy.qty
    assert result.final_cash == _money(payload, "take_profit_cash")
    assert result.final_equity == result.final_cash
    assert result.final_positions == ()


def test_stop_loss_fills_at_the_stop_when_the_bar_opens_above_it() -> None:
    """Same entry. The next bar trades 90–100 and opens at 100.

    Stop = 95.0475. Sell fill = 94.99997625.
    Notional = 9485.76. Commission = 9.49. Cash = 9476.27.
    """
    payload, document = _oco_hand()
    result = run_backtest(
        document,
        {"sol": [_fixture_bar(payload["entry"]), _fixture_bar(payload["stop_loss_bar"])]},
    )
    assert [fill.leg for fill in result.fills] == ["market", "stop_loss"]
    sell = result.fills[1]
    assert sell.price == _money(payload, "stop_loss_fill")
    assert sell.notional == _money(payload, "stop_loss_notional")
    assert sell.commission == _money(payload, "stop_loss_commission")
    assert result.final_cash == _money(payload, "stop_loss_cash")
    assert result.final_positions == ()


def test_both_levels_in_one_bar_fill_the_stop_and_cancel_the_target() -> None:
    """Open 100, high 130, low 80. Both legs are touched. The stop fills.

    A later bar trades up to 200. The cancelled target does not sell again.
    The cents match the stop-loss case: the open was above the stop.
    """
    payload, document = _oco_hand()
    later = _bar(120, 150, open_=140, high=200, low=140)
    result = run_backtest(
        document,
        {
            "sol": [
                _fixture_bar(payload["entry"]),
                _fixture_bar(payload["both_bar"]),
                later,
            ]
        },
    )
    assert [fill.leg for fill in result.fills] == ["market", "stop_loss"]
    assert result.fills[1].open_time == _START + 60
    assert result.fills[1].price == _money(payload, "stop_loss_fill")
    assert result.fills[1].notional == _money(payload, "stop_loss_notional")
    assert result.final_cash == _money(payload, "stop_loss_cash")
    assert result.final_positions == ()


def test_a_stop_that_gaps_through_fills_at_the_open() -> None:
    """The bar opens at 80, through the 95.0475 stop. The raw price is 80.

    Sell fill = 79.96. Notional = 7984.02. Commission = 7.98. Cash = 7976.04.
    """
    payload, document = _oco_hand()
    result = run_backtest(
        document,
        {"sol": [_fixture_bar(payload["entry"]), _fixture_bar(payload["gap_bar"])]},
    )
    sell = result.fills[1]
    assert sell.leg == "stop_loss"
    assert sell.price == _money(payload, "gap_fill")
    assert sell.notional == _money(payload, "gap_notional")
    assert sell.commission == _money(payload, "gap_commission")
    assert result.final_cash == _money(payload, "gap_cash")


def test_a_take_profit_gap_still_fills_at_the_limit() -> None:
    payload, document = _oco_hand()
    gap_up = _bar(60, 140, open_=130, high=150, low=130)
    result = run_backtest(
        document, {"sol": [_fixture_bar(payload["entry"]), gap_up]}
    )
    assert result.fills[1].leg == "take_profit"
    assert result.fills[1].price == _money(payload, "take_profit_fill")
    assert result.final_cash == _money(payload, "take_profit_cash")


def test_a_synthetic_minute_does_not_fill_and_the_next_real_bar_can_gap() -> None:
    """A flat synthetic bar at 90 is not a touch and does not fill.

    The next real bar opens at 92, below the 95.0475 stop, so the stop gaps
    through at the open. Sell fill = 91.954. Notional = 9181.62.
    Commission = 9.18. Cash = 9172.44.
    """
    payload, document = _oco_hand()
    synthetic = _bar(60, 90, synthetic=True)
    real = _fixture_bar(payload["after_synthetic_bar"])
    held = run_backtest(document, {"sol": [_fixture_bar(payload["entry"]), synthetic]})
    assert [fill.leg for fill in held.fills] == ["market"]
    assert len(held.final_positions) == 1
    assert held.final_cash == Decimal("0.00")

    result = run_backtest(
        document, {"sol": [_fixture_bar(payload["entry"]), synthetic, real]}
    )
    assert [fill.open_time for fill in result.fills] == [_START, _START + 120]
    sell = result.fills[1]
    assert sell.leg == "stop_loss"
    assert sell.price == _money(payload, "after_synthetic_fill")
    assert sell.notional == _money(payload, "after_synthetic_notional")
    assert sell.commission == _money(payload, "after_synthetic_commission")
    assert result.final_cash == _money(payload, "after_synthetic_cash")
    assert result.final_positions == ()


def test_a_synthetic_minute_and_a_hole_leave_the_oco_for_the_next_real_bar() -> None:
    """A synthetic bar flat at 90 is not a touch. A hole is not a touch.

    The following real bar opens at 100 and trades 100–101, above the stop.
    Neither run fills. The long stays open.
    """
    payload, document = _oco_hand()
    synthetic = _bar(60, 90, synthetic=True)
    resumed = _bar(180, 101, open_=100, high=101, low=100)
    with_minute = run_backtest(
        document,
        {"sol": [_fixture_bar(payload["entry"]), synthetic, resumed]},
    )
    hole = run_backtest(
        document,
        {"sol": [_fixture_bar(payload["entry"]), resumed]},
    )
    assert [fill.leg for fill in with_minute.fills] == ["market"]
    assert with_minute.fills == hole.fills
    assert with_minute.final_positions == hole.final_positions
    assert len(with_minute.final_positions) == 1
    assert with_minute.final_cash == Decimal("0.00")


def test_a_future_stop_does_not_change_earlier_equity() -> None:
    payload, document = _oco_hand()
    entry = _fixture_bar(payload["entry"])
    quiet = _bar(60, 101, open_=100, high=101, low=100)
    # Close 96 does not match the entry signal, so the stop is the last fill.
    stopped = _bar(120, 96, open_=100, high=100, low=1)
    held = _bar(120, 101, open_=100, high=101, low=100)
    hit = run_backtest(document, {"sol": [entry, quiet, stopped]})
    miss = run_backtest(document, {"sol": [entry, quiet, held]})
    assert [fill for fill in hit.fills if fill.open_time < _START + 120] == [
        fill for fill in miss.fills if fill.open_time < _START + 120
    ]
    assert [point for point in hit.equity_curve if point.open_time < _START + 120] == [
        point for point in miss.equity_curve if point.open_time < _START + 120
    ]
    assert hit.fills[-1].leg == "stop_loss"
    assert [fill.leg for fill in miss.fills] == ["market"]


def test_a_partial_exit_leaves_the_oco_on_the_remainder() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "stop", "expr": _eq("close", 100)},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(10, 5),
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 50},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    result = run_backtest(
        document,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 96, open_=100, high=100, low=90),
            ]
        },
    )
    assert [fill.leg for fill in result.fills] == ["market", "market", "stop_loss"]
    assert result.fills[1].qty * 2 == result.fills[0].qty
    assert result.fills[2].qty == result.fills[1].qty
    assert result.fills[2].price == Decimal("95")
    assert result.fills[2].notional == Decimal("4750.00")
    assert result.final_cash == Decimal("9750.00")
    assert result.final_positions == ()


def test_tighten_only_rejects_a_looser_level_and_allows_a_tighter_one() -> None:
    both = "tighten_only rejected a looser stop_loss and take_profit"
    with pytest.raises(BacktestError, match=both):
        move_oco(Decimal("110"), Decimal("90"), Decimal("120"), Decimal("80"))
    with pytest.raises(BacktestError, match="tighten_only rejected a looser stop_loss"):
        move_oco(Decimal("110"), Decimal("90"), Decimal("104"), Decimal("80"))
    with pytest.raises(BacktestError, match="tighten_only rejected a looser take_profit"):
        move_oco(Decimal("110"), Decimal("90"), Decimal("120"), Decimal("96"))
    assert move_oco(Decimal("110"), Decimal("90"), Decimal("104"), Decimal("96")) == (
        Decimal("104"),
        Decimal("96"),
    )

    # Both enters fill on the close-100 bar, before any OCO check. Anchor 100,
    # 10% levels 110 and 90. The second enter's 20% levels would be 120 and
    # 80. Both loosen, so the stop stays 90. The next bar opens at 95 and
    # trades down to 85: fill at 90. A stop at 80 would not be touched.
    # Quantity is 100. Notional = 9000. Cash = 9000.
    loose = _pyramid_oco(_risk(20, 20))
    loose_result = run_backtest(
        loose,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 92, open_=95, high=95, low=85),
            ]
        },
    )
    assert any(
        both in note and " on sol at bar " in note and str(_START) in note
        for note in loose_result.lints
    )
    assert [fill.leg for fill in loose_result.fills] == ["market", "market", "stop_loss"]
    assert loose_result.fills[2].price == Decimal("90")
    assert loose_result.fills[2].notional == Decimal("9000.00")
    assert loose_result.final_cash == Decimal("9000.00")

    # 4% on the same anchor is 104 and 96. The next bar trades 95–100 and
    # opens at 100, so the tighter stop fills at 96. The old stop at 90
    # would not. Notional = 100 * 96 = 9600. Cash = 9600.
    tight = _pyramid_oco(_risk(4, 4))
    tight_result = run_backtest(
        tight,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 98, open_=100, high=100, low=95),
            ]
        },
    )
    assert tight_result.lints == ()
    assert tight_result.fills[2].leg == "stop_loss"
    assert tight_result.fills[2].price == Decimal("96")
    assert tight_result.fills[2].notional == Decimal("9600.00")
    assert tight_result.final_cash == Decimal("9600.00")


def test_inserting_a_synthetic_minute_does_not_change_the_oco_exit() -> None:
    """A no-trade minute must not relabel or invent an exit.

    Case A: the pyramid tightens the target from 120 to 105 while price is
    108, and the stop stays 97. A synthetic minute flat at 108 used to arm
    the target, so the next bar's open at 95 filled as take_profit. Without
    that minute the same bar is a gap-through stop at 95.

    Case C: 50 bps of buy slippage puts a 0.3% stop at 100.1985, above the
    entry close. A synthetic copy of that close used to arm the stop, and the
    next bar (100.8–101.5) filled at 100.1985, a price it never traded.
    Without the minute there is no exit.
    """
    tightened = _pyramid_levels(_risk(20, 3), _risk(5, 3))
    entry = _bar(0, 100)
    added = _bar(60, 108)
    quiet = _bar(120, 108, synthetic=True)
    gap = _bar(180, 95, open_=95, high=95, low=90)
    with_minute = run_backtest(tightened, {"sol": [entry, added, quiet, gap]})
    without = run_backtest(tightened, {"sol": [entry, added, gap]})
    assert [fill.leg for fill in without.fills] == ["market", "market", "stop_loss"]
    assert without.fills[-1].price == Decimal("95")
    assert with_minute.fills == without.fills
    assert with_minute.final_cash == without.final_cash
    assert with_minute.final_positions == ()

    slipped = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 50},
        signals=[{"id": "go", "expr": _eq("close", 100)}],
    )
    slipped["actions"][0]["risk"] = _risk(6, 0.3)
    bought = _bar(0, 100)
    copied = _bar(60, 100, synthetic=True)
    traded = _bar(120, 101.2, open_=101.0, high=101.5, low=100.8)
    armed = run_backtest(slipped, {"sol": [bought, copied, traded]})
    plain = run_backtest(slipped, {"sol": [bought, traded]})
    assert [fill.leg for fill in plain.fills] == ["market"]
    assert plain.fills[0].price == Decimal("100.5")
    assert len(plain.final_positions) == 1
    assert armed.fills == plain.fills
    assert armed.final_positions == plain.final_positions
    assert all(fill.price != Decimal("100.1985") for fill in armed.fills)


def test_oco_settles_before_the_same_bars_signal_exit() -> None:
    """The stop fills before rules, so the signal exit finds no position.

    Moving settlement to after the rules would sell at the close instead,
    and the skip would disappear.
    """
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "stop", "expr": _eq("close", 96)},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    document["actions"][0]["risk"] = _risk(10, 5)
    result = run_backtest(
        document,
        {"sol": [_bar(0, 100), _bar(60, 96, open_=100, high=100, low=90)]},
    )
    assert [fill.leg for fill in result.fills] == ["market", "stop_loss"]
    assert result.fills[1].price == Decimal("95")
    assert [(item.action_id, item.reason) for item in result.skipped] == [
        ("exit_sol", "no_position")
    ]


def test_a_level_touched_at_equality_fills() -> None:
    """low == stop fills the stop. high == target fills the target."""
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": _eq("close", 100)}],
    )
    document["actions"][0]["risk"] = _risk(10, 5)
    entry = _bar(0, 100)
    stopped = run_backtest(
        document,
        {"sol": [entry, _bar(60, 96, open_=100, high=100, low=95)]},
    )
    targeted = run_backtest(
        document,
        {"sol": [entry, _bar(60, 105, open_=100, high=110, low=100)]},
    )
    assert [fill.leg for fill in stopped.fills] == ["market", "stop_loss"]
    assert stopped.fills[1].price == Decimal("95")
    assert [fill.leg for fill in targeted.fills] == ["market", "take_profit"]
    assert targeted.fills[1].price == Decimal("110")


def test_a_same_bar_exit_and_reenter_anchors_a_fresh_oco() -> None:
    """A full exit drops the pair. The re-enter on that bar starts a new one.

    The new fill is 120, so a 3% stop is 116.4. The old pair was anchored at
    100 with a stop at 1, and a low of 116 would not reach it.
    """
    enter = {
        "type": "enter",
        "side": "long",
        "asset": "sol",
        "order": {"type": "market"},
        "sizing": {"mode": "percent_equity", "percent": 100},
    }
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "out", "expr": _eq("close", 120)},
            {"id": "again", "expr": _eq("close", 120)},
        ],
        actions=[
            {"id": "enter_sol", **enter, "risk": _risk(100, 99)},
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
            {"id": "reenter_sol", **enter, "risk": _risk(6, 3)},
        ],
        rules=[
            {"id": "exit_rule", "when": "out", "do": "exit_sol"},
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "reenter_rule", "when": "again", "do": "reenter_sol"},
        ],
    )
    result = run_backtest(
        document,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 120),
                _bar(120, 117, open_=118, high=118, low=116),
            ]
        },
    )
    assert [fill.leg for fill in result.fills] == ["market", "market", "market", "stop_loss"]
    assert result.fills[-1].price == Decimal("116.4")
    assert result.final_positions == ()


def test_a_pyramid_keeps_the_first_entry_anchor() -> None:
    """Same percents stay on the first fill. 10% of 100 is 110, not 115.5.

    The added fill is 105. A target anchored there would be 115.5, and a
    high of 112 would not reach it.
    """
    document = _pyramid_oco(_risk(10, 10))
    document["signals"] = [
        {"id": "go", "expr": _eq("close", 100)},
        {"id": "add", "expr": _eq("close", 105)},
    ]
    result = run_backtest(
        document,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 105),
                _bar(120, 108, open_=100, high=112, low=100),
            ]
        },
    )
    assert [fill.leg for fill in result.fills] == ["market", "market", "take_profit"]
    assert result.fills[2].price == Decimal("110")
    assert result.final_positions == ()


def _pyramid_levels(first: dict, second: dict) -> dict:
    """50% at close 100, then 50% at close 108, on the first fill's anchor."""
    enter = {
        "type": "enter",
        "side": "long",
        "asset": "sol",
        "order": {"type": "market"},
        "sizing": {"mode": "percent_equity", "percent": 50},
    }
    return _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "add", "expr": _eq("close", 108)},
        ],
        actions=[
            {"id": "enter_sol", **enter, "risk": first},
            {"id": "add_sol", **enter, "risk": second},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "add_rule", "when": "add", "do": "add_sol"},
        ],
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 1,
            "allow_pyramiding": True,
            "quote_currency": "USDC",
        },
    )


def _pyramid_oco(second_risk: dict) -> dict:
    """Two 50% enters on the close-100 bar, then a later bar that can stop out.

    The second enter is on the entry bar, so the OCO is not evaluated until
    the next bar. A later close outside the band would stop out before the
    add, because low is at or below that close.
    """
    enter = {
        "type": "enter",
        "side": "long",
        "asset": "sol",
        "order": {"type": "market"},
        "sizing": {"mode": "percent_equity", "percent": 50},
    }
    return _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "add", "expr": _eq("close", 100)},
        ],
        actions=[
            {"id": "enter_sol", **enter, "risk": _risk(10, 10)},
            {"id": "add_sol", **enter, "risk": second_risk},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "add_rule", "when": "add", "do": "add_sol"},
        ],
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 1,
            "allow_pyramiding": True,
            "quote_currency": "USDC",
        },
    )


def test_move_oco_rejects_a_non_finite_level() -> None:
    with pytest.raises(BacktestError, match="not a finite price"):
        move_oco(Decimal("110"), Decimal("90"), Decimal("NaN"), Decimal("96"))
    with pytest.raises(BacktestError, match="not a finite price"):
        move_oco(Decimal("110"), Decimal("90"), Decimal("Infinity"), Decimal("96"))


def test_a_tiny_percent_does_not_pin_the_oco_to_the_entry() -> None:
    document = _strategy(signals=[{"id": "go", "expr": {"op": "lit", "value": True}}])
    document["actions"][0]["risk"] = _risk(1e-300, 1e-300)
    with pytest.raises(BacktestError, match="too small to move the price"):
        run_backtest(document, {"sol": [_bar(0, 100)]})


def test_a_mixed_tighten_and_loosen_is_rejected_as_a_whole() -> None:
    """One tighter leg must not stick when the other leg loosens.

    Anchor 100, 10% levels 110 and 90. The add asks for 5% and 20%: the
    target would be 105 and the stop 80. The stop loosens, so both levels
    stay. A partial accept of the target would sell a high of 106 at 105.
    A partial accept of the stop would ignore a low of 85.
    """
    mixed = _pyramid_oco(_risk(5, 20))
    held = run_backtest(
        mixed,
        {"sol": [_bar(0, 100), _bar(60, 99, open_=100, high=106, low=92)]},
    )
    assert any("looser stop_loss" in note and "take_profit" not in note for note in held.lints)
    assert any(f"on sol at bar {_START}" in note for note in held.lints)
    assert [fill.leg for fill in held.fills] == ["market", "market"]
    assert len(held.final_positions) == 1

    stopped = run_backtest(
        mixed,
        {"sol": [_bar(0, 100), _bar(60, 96, open_=95, high=100, low=85)]},
    )
    assert [fill.leg for fill in stopped.fills] == ["market", "market", "stop_loss"]
    assert stopped.fills[2].price == Decimal("90")


def test_a_pyramid_below_the_anchor_uses_the_first_fill() -> None:
    """The added fill is 96, under the 100 anchor, and the percents differ.

    10% of 100 is 110. 10% of 96 is 105.6, and that stop at 93.12 would
    loosen, so a wrong anchor keeps 120 and 95 and does not sell a high of
    110. The first fill's 10% target does.
    """
    document = _pyramid_levels(_risk(20, 5), _risk(10, 3))
    document["signals"] = [
        {"id": "go", "expr": _eq("close", 100)},
        {"id": "add", "expr": _eq("close", 96)},
    ]
    result = run_backtest(
        document,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 96),
                _bar(120, 108, open_=100, high=110, low=98),
            ]
        },
    )
    assert result.lints == ()
    assert [fill.leg for fill in result.fills] == ["market", "market", "take_profit"]
    assert result.fills[2].price == Decimal("110")
    assert result.final_positions == ()


def test_fill_quantity_rounds_down_not_half_even() -> None:
    """5000 / 3 sits 2/3 of the way to the next 1e-12 unit. Half-even rounds up."""
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            }
        ],
    )
    result = run_backtest(document, {"sol": [_bar(0, 3)]})
    scaled = Fraction(5000) / Fraction(3) * 10**12
    units = scaled.numerator // scaled.denominator
    expected = Decimal((0, tuple(map(int, str(units))), -12))
    # The remainder is 2/3 of a unit, so half-even is the next unit up.
    rounded_up = Decimal((0, tuple(map(int, str(units + 1))), -12))
    assert expected == Decimal("1666.666666666666")
    assert rounded_up == Decimal("1666.666666666667")
    assert result.fills[0].qty == expected
    assert result.fills[0].notional == Decimal("5000.00")


def test_floor_qty_stays_down_past_fifty_digits() -> None:
    """A 61-digit unit count. ``scaleb`` keeps the tail when the context can hold it."""
    units = 10**60 + 5
    with localcontext() as ctx:
        ctx.prec = 80
        floored = floor_qty(Fraction(units, 10**12))
    exact = Decimal((0, tuple(map(int, str(units))), -12))
    assert floored == exact
    assert str(floored).endswith("000000000005")


def test_floor_qty_rejects_a_quantity_the_context_cannot_hold() -> None:
    units = 10**60 + 5
    with localcontext() as ctx:
        ctx.prec = 50
        with pytest.raises(BacktestError, match="61 digits") as caught:
            floor_qty(Fraction(units, 10**12))
    assert "do not match" not in caught.value.message


def test_commission_is_clamped_when_the_room_rounds_up() -> None:
    """A one-cent budget at 9999 bps. The unclamped commission is the whole cent.

    Room is half-even of 0.01 * 10000 / 19999, which rounds up to 0.01.
    Notional is 0.01 and the rounded commission is another 0.01. The clamp
    sets commission to zero so the account does not go negative.
    """
    document = _strategy(
        capital={"initial": 0.01, "currency": "USDC"},
        costs={"commission_bps": 9999, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    result = run_backtest(document, {"sol": [_bar(0, 1)]})
    assert result.fills[0].notional == Decimal("0.01")
    assert result.fills[0].commission == Decimal("0.00")
    assert result.final_cash == Decimal("0.00")
    assert min(point.cash for point in result.equity_curve) == Decimal("0.00")


def test_trade_span_counts_the_entry_bar_and_not_the_bar_before_it() -> None:
    """The flagged bar before the entry is outside the trade. The entry bar is inside."""
    grid = [0, 60, 120, 180, 240]
    # Before the entry, the entry, a quiet middle, the exit, and nothing past it.
    prefix = _flag_prefix([False, True, True, False, True])
    assert _between(prefix, grid, 120, 240) == 2

    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "stop", "expr": _eq("close", 110)},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(
        document,
        {
            "sol": [
                _bar(0, 50),
                _bar(120, 100),
                _bar(180, 100, synthetic=True),
                _bar(240, 110),
            ]
        },
    )
    trade = result.trades[0]
    assert trade.entry_time == _START + 120
    assert trade.exit_time == _START + 240
    assert trade.missing_bars == 0
    assert trade.synthetic_bars == 1


def _entry_band(low: int, high: int, close: int) -> dict:
    """True when ENTRY_PRICE is inside (low, high) and the close matches."""
    entry = {"op": "entry_price", "args": []}
    return {
        "op": "and",
        "left": {
            "op": "and",
            "left": {"op": "gt", "left": entry, "right": {"op": "lit", "value": low}},
            "right": {"op": "lt", "left": entry, "right": {"op": "lit", "value": high}},
        },
        "right": _eq("close", close),
    }


def test_entry_price_updates_after_an_add() -> None:
    """50 at 100, then 40 at 125. The average is 10000/90, not 100 and not 125.

    The take-profit is 200, so the add at 125 does not close the long. The
    next bar sells only when ENTRY_PRICE is between 105 and 120.
    """
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "add", "expr": _eq("close", 125)},
            {"id": "check", "expr": _entry_band(105, 120, 130)},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            },
            {
                "id": "add_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(100, 99),
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "add_rule", "when": "add", "do": "add_sol"},
            {"id": "check_rule", "when": "check", "do": "exit_sol"},
        ],
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 1,
            "allow_pyramiding": True,
            "quote_currency": "USDC",
        },
    )
    held = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 125)]})
    position = held.final_positions[0]
    assert position.qty == Decimal("90")
    assert position.entry_price == decimal_ratio(Fraction(10000) / Fraction(90))
    checked = run_backtest(
        document, {"sol": [_bar(0, 100), _bar(60, 125), _bar(120, 130)]}
    )
    assert [fill.side for fill in checked.fills] == ["buy", "buy", "sell"]
    assert checked.final_positions == ()


def test_entry_price_stays_the_average_after_a_partial_trim() -> None:
    """Selling 25% leaves the average at 100. Leaving the cost unreduced makes it 133.33.

    The next bar sells only when ENTRY_PRICE is still between 50 and 110.
    """
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "trim", "expr": _eq("close", 130)},
            {"id": "check", "expr": _entry_band(50, 110, 140)},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(100, 99),
            },
            {
                "id": "trim_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 25},
            },
            {
                "id": "exit_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "trim_rule", "when": "trim", "do": "trim_sol"},
            {"id": "check_rule", "when": "check", "do": "exit_sol"},
        ],
    )
    held = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 130)]})
    position = held.final_positions[0]
    assert position.qty == Decimal("75")
    assert position.entry_price == Decimal("100")
    checked = run_backtest(
        document, {"sol": [_bar(0, 100), _bar(60, 130), _bar(120, 140)]}
    )
    assert [fill.qty for fill in checked.fills] == [
        Decimal("100"),
        Decimal("25"),
        Decimal("75"),
    ]
    assert checked.final_positions == ()


def test_a_partial_exit_that_rounds_to_zero_leaves_the_dust_open() -> None:
    """One 1e-12 unit. Half of it floors to zero, so the exit is no_position."""
    document = _strategy(
        capital={"initial": 1, "currency": "USDC"},
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": {"op": "lit", "value": True}},
            {"id": "trim", "expr": {"op": "lit", "value": True}},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 100},
                "risk": _risk(100, 99),
            },
            {
                "id": "trim_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 50},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "trim_rule", "when": "trim", "do": "trim_sol"},
        ],
    )
    result = run_backtest(document, {"sol": [_bar(0, 1_000_000_000_000)]})
    assert result.fills[0].qty == Decimal("0.000000000001")
    assert [item.reason for item in result.skipped] == ["no_position"]
    assert result.final_positions[0].qty == Decimal("0.000000000001")


def test_a_same_bar_reenter_after_an_oco_fill_anchors_a_new_pair() -> None:
    """The stop closes the long, and the enter on that bar starts a new one.

    The new fill is 96, so a 5% stop is 91.2. The old stop was 95, and this
    bar opens at 94, so the old pair would have sold at the open.
    """
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {
                "id": "go",
                "expr": {
                    "op": "or",
                    "left": _eq("close", 100),
                    "right": _eq("close", 96),
                },
            }
        ],
    )
    document["actions"][0]["risk"] = _risk(10, 5)
    result = run_backtest(
        document,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 96, open_=100, high=100, low=90),
                _bar(120, 93, open_=94, high=94, low=91),
            ]
        },
    )
    assert [fill.leg for fill in result.fills] == ["market", "stop_loss", "market", "stop_loss"]
    assert result.fills[1].price == Decimal("95")
    assert result.fills[3].price == Decimal("91.2")
    assert result.final_positions == ()


def test_a_year_of_one_minute_slots_is_rejected_before_the_grid() -> None:
    document = _strategy()
    year = 365 * 86400
    started = time.perf_counter()
    with pytest.raises(BacktestError) as caught:
        run_backtest(document, {"sol": [_bar(0, 1.0), _bar(year, 1.0)]})
    assert time.perf_counter() - started < 1
    assert caught.value.code == "grid_too_large"


def test_the_engine_ignores_a_caller_inexact_trap() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            }
        ],
    )
    bars = [_bar(0, 3)]
    plain = run_backtest(document, {"sol": bars})
    with localcontext() as ctx:
        ctx.traps[Inexact] = True
        trapped = run_backtest(document, {"sol": bars})
        assert getcontext().traps[Inexact]
    assert trapped.fills[0].qty == plain.fills[0].qty == Decimal("1666.666666666666")


def test_the_engine_ignores_a_caller_emin() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            }
        ],
    )
    bars = [_bar(0, 3)]
    plain = run_backtest(document, {"sol": bars})
    with localcontext() as ctx:
        ctx.Emin = -5
        trapped = run_backtest(document, {"sol": bars})
    assert trapped.fills[0].qty == plain.fills[0].qty == Decimal("1666.666666666666")


def test_a_quantity_past_context_precision_is_a_clear_error() -> None:
    """One buy. 1e12 capital at about 1.2e-27 is more than 50 digits of units."""
    document = _strategy(
        capital={"initial": 1_000_000_000_000, "currency": "USDC"},
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    with pytest.raises(BacktestError) as caught:
        run_backtest(document, {"sol": [_bar(0, 1.2e-27)]})
    assert "precision" in caught.value.message
    assert "do not match" not in caught.value.message


def test_indicator_lints_stay_on_the_run_that_produced_them() -> None:
    """Two threads. Flat stochastic lints do not land on the quiet run."""
    import warnings
    from concurrent.futures import ThreadPoolExecutor

    flat = _strategy(
        derived_series=[
            {
                "id": "stoch_k",
                "kind": "indicator",
                "indicator": "stochastic",
                "source": "close",
                "output": "k",
                "params": {"k_period": 1, "d_period": 1},
            }
        ]
    )
    quiet = _strategy()
    flat_bars = [_bar(0, 1.0), _bar(60, 1.5, high=2.0, low=0.5)]
    quiet_bars = [_bar(0, 1.0), _bar(60, 2.0)]
    before = list(warnings.filters)

    def one(kind: str) -> tuple[str, ...]:
        if kind == "flat":
            return run_backtest(flat, {"sol": flat_bars}).lints
        return run_backtest(quiet, {"sol": quiet_bars}).lints

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, "flat" if index % 2 == 0 else "quiet") for index in range(40)]
        results = [item.result() for item in futures]
    assert warnings.filters == before
    for index, lints in enumerate(results):
        if index % 2 == 0:
            assert any("highest high equals the lowest low" in note for note in lints)
        else:
            assert lints == ()
            assert not any("highest high" in note for note in lints)
