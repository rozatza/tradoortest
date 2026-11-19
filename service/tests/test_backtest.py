"""Backtest engine: cents, warmup, holes, synthetic bars, and no look-ahead.

The hand-calc fixture is synthetic. Nothing here calls the network.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.backtest import BacktestError, run_backtest
from app.ohlcv.bars import Bar

_START = 1_767_225_600  # 2026-01-01T00:00:00Z
_MINT = "SyntheticMint111111111111111111111111111"
_FIXTURE = Path(__file__).parent / "fixtures" / "backtest" / "hand_round_trip.json"

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


def _risk() -> dict:
    return {
        "type": "oco",
        "evaluation": "on_entry_fill",
        "movement": "tighten_only",
        "take_profit": {"mode": "percent", "percent": 6},
        "stop_loss": {"mode": "percent", "percent": 3},
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
    is warmup, so it does not trade. The entry carries an OCO block because
    IR requires one; this engine does not fill it.

    Buy close 100. Fill = 100 * 10005/10000 = 100.05.
    Budget = 50% of 10000.00 = 5000.00.
    Notional = round_half_even(5000 * 10000/10010) = round_half_even(4995.004995...) = 4995.00.
    Commission = 5000.00 - 4995.00 = 5.00. Quantity = 4995.00 / 100.05.
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
    assert buy.qty == _BUY_NOTIONAL / _BUY_PRICE
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


def test_stop_loss_in_the_ir_does_not_exit() -> None:
    # Stop is 3% under the fill. The next bar trades at 50, through that stop.
    # There is no exit signal. The long stays open.
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
    assert len(result.fills) == 1
    assert result.fills[0].side == "buy"
    assert len(result.final_positions) == 1
    assert result.final_positions[0].qty == result.fills[0].qty


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
