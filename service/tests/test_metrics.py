"""Metrics, the trade log, and the data-quality counts.

The hand arithmetic is in the test docstrings. The bars are synthetic.
Nothing here calls the network.
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import subprocess
import sys
import time
from decimal import ROUND_HALF_EVEN, Decimal, Inexact, InvalidOperation, localcontext
from fractions import Fraction

from app.backtest import (
    METRIC_COLUMNS,
    TRADE_COLUMNS,
    BacktestError,
    metrics_csv,
    metrics_json,
    run_backtest,
    trade_log_csv,
    trade_log_json,
)
from app.backtest.engine import _spread_remainder
from app.backtest.log import METRICS_SCHEMA, SCHEMA
from app.backtest.metrics import Trade, compute_metrics
from app.backtest.money import decimal_ratio, floor_qty, quantize_cents
from app.backtest.quality import QualityCounts
from app.ohlcv.bars import Bar
from test_backtest import (
    _FINAL_EQUITY,
    _FIXTURE,
    _MINT,
    _OCO_FIXTURE,
    _START,
    _bar,
    _eq,
    _fixture_bar,
    _gt,
    _money,
    _risk,
    _strategy,
)

_RATIO_NAMES = (
    "net_return",
    "annualized_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "payoff_ratio",
)


def _finite(result) -> None:
    for name in _RATIO_NAMES:
        value = getattr(result.metrics, name)
        if value is None:
            assert result.metrics.undefined[name]
        else:
            assert value.is_finite()
            assert name not in result.metrics.undefined
            assert "E" not in format(value, "f")
    for trade in result.trades:
        assert trade.realized_pnl.is_finite()
        assert trade.unrealized_pnl.is_finite()


def _logs_agree(csv_text: str, payload_rows: list[dict]) -> None:
    """An empty CSV cell is the JSON null. Every other cell is the same string."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == len(payload_rows)
    assert "\r" not in csv_text
    for csv_row, json_row in zip(rows, payload_rows, strict=True):
        assert list(csv_row) == list(TRADE_COLUMNS)
        assert list(json_row) == list(TRADE_COLUMNS)
        for column in TRADE_COLUMNS:
            if json_row[column] is None:
                assert csv_row[column] == ""
            else:
                assert csv_row[column] == json_row[column]


def _flat(**overrides: object) -> dict:
    """A book that does not trade. The signal is never true."""
    return _strategy(
        signals=[{"id": "go", "expr": _gt("close", 10_000)}],
        **overrides,
    )


def _round_trip_document() -> tuple[dict, list[Bar]]:
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
    return document, bars


def test_half_even_cents_use_the_exact_rational() -> None:
    """5049.495 is an exact half-cent. The kept digit 9 is odd, so the cent is 5049.50.

    5049.485 keeps the even digit 8.
    """
    assert quantize_cents(Fraction("5049.495")) == Decimal("5049.50")
    assert quantize_cents(Fraction("5049.485")) == Decimal("5049.48")
    assert quantize_cents(Fraction("-5049.495")) == Decimal("-5049.50")


def test_a_sale_on_an_exact_half_cent_does_not_use_the_50_digit_quantity() -> None:
    """43175 / 78.834 * 1.655514 = 906.675 exactly, which would be 906.68.

    The fill quantity is that quotient rounded down to 1e-12. Times the sale
    price it is 906.6749999999994, and half-even keeps 906.67.
    """
    document = _strategy(
        capital={"initial": 43175, "currency": "USDC"},
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _gt("close", 10)},
            {
                "id": "stop",
                "expr": {
                    "op": "lt",
                    "left": {"op": "ref", "name": "close"},
                    "right": {"op": "lit", "value": 10},
                },
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(
        document,
        {"sol": [_bar(0, 78.834), _bar(60, 1.655514)]},
    )
    assert result.fills[1].leg == "market"
    assert result.fills[1].notional == Decimal("906.67")
    assert result.final_cash == Decimal("906.67")
    assert result.trades[0].exit_notional == Decimal("906.67")
    assert result.trades[0].realized_pnl == Decimal("-42268.33")
    _finite(result)


def test_hand_round_trip_metrics_and_trade_log() -> None:
    """Equity 10000, 10000, 9992.50, 11480.52.

    Net return = 1480.52 / 10000 = 0.148052.
    Max drawdown = 7.50 / 10000 = 0.00075.
    The trade's cash pnl is 11480.52 - 10000 = 1480.52.
    """
    document, bars = _round_trip_document()
    result = run_backtest(document, {"sol": bars})
    _finite(result)
    assert result.final_equity == _FINAL_EQUITY
    assert result.metrics.net_return == Decimal("0.148052")
    assert result.metrics.max_drawdown == Decimal("0.00075")
    assert result.metrics.bars_per_year == 525_600
    assert result.metrics.bar_count == 3
    assert result.metrics.closed_trades == 1
    assert result.metrics.open_trades == 0
    assert result.metrics.win_rate == Decimal("1")
    assert result.metrics.win_streak == 1
    assert result.metrics.loss_streak == 0
    assert result.metrics.payoff_ratio is None
    assert result.metrics.undefined["payoff_ratio"] == "no_losses"
    assert result.metrics.annualized_return is None
    assert result.metrics.undefined["annualized_return"] == "too_short"
    assert result.metrics.undefined["calmar"] == "too_short"
    assert result.metrics.suspect_outage is False
    trade = result.trades[0]
    assert trade.status == "closed"
    assert trade.entry_notional == Decimal("4995.00")
    assert trade.exit_notional == Decimal("6487.01")
    assert trade.commission == Decimal("11.49")
    assert trade.realized_pnl == Decimal("1480.52")
    assert trade.unrealized_pnl == Decimal("0.00")
    assert trade.limit_below_low is False
    assert trade.exit_time == _START + 120


def test_csv_and_json_trade_logs_agree() -> None:
    document, bars = _round_trip_document()
    result = run_backtest(document, {"sol": bars})
    csv_text = trade_log_csv(result.trades)
    payload = json.loads(trade_log_json(result.trades))
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert payload["schema"] == SCHEMA
    assert list(rows[0]) == list(TRADE_COLUMNS)
    assert list(payload["trades"][0]) == list(TRADE_COLUMNS)
    _logs_agree(csv_text, payload["trades"])
    assert rows[0]["realized_pnl"] == "1480.52"
    assert rows[0]["limit_below_low"] == "false"
    assert rows[0]["entry_time"] == str(_START + 60)


def test_an_empty_trade_log_still_agrees() -> None:
    result = run_backtest(_flat(), {"sol": [_bar(0, 1), _bar(60, 1)]})
    rows = list(csv.DictReader(io.StringIO(trade_log_csv(result.trades))))
    payload = json.loads(trade_log_json(result.trades))
    assert rows == payload["trades"] == []
    assert trade_log_csv(result.trades).splitlines() == [",".join(TRADE_COLUMNS)]


def test_zero_trades_and_zero_variance_are_finite_zeros() -> None:
    result = run_backtest(_flat(), {"sol": [_bar(0, 1), _bar(60, 1), _bar(120, 1)]})
    _finite(result)
    assert result.fills == ()
    assert result.metrics.net_return == Decimal("0")
    assert result.metrics.max_drawdown == Decimal("0")
    assert result.metrics.undefined["sharpe"] == "zero_variance"
    assert result.metrics.undefined["sortino"] == "no_downside"
    assert result.metrics.undefined["annualized_return"] == "too_short"
    assert result.metrics.undefined["calmar"] == "too_short"
    assert result.metrics.undefined["win_rate"] == "no_closed_trades"
    assert result.metrics.undefined["payoff_ratio"] == "no_closed_trades"
    assert "0E-10" not in metrics_json(result.metrics)
    assert format(result.metrics.net_return, "f") in metrics_json(result.metrics)
    assert result.metrics.win_streak == 0
    assert result.metrics.loss_streak == 0
    assert result.metrics.closed_trades == 0
    assert result.metrics.bar_count == 3


def test_no_losses_leave_payoff_sortino_undefined() -> None:
    """One winning sale on two minutes. Equity never drops, and no return is negative.

    Payoff has no loss to divide by. Sortino has no downside. The run is
    shorter than a day, so Calmar is too_short rather than no_drawdown.
    Sharpe is defined: the two returns are 0 and 0.1.
    """
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
    result = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 110)]})
    _finite(result)
    assert result.metrics.closed_trades == 1
    assert result.metrics.win_rate == Decimal("1")
    assert result.metrics.undefined["payoff_ratio"] == "no_losses"
    assert result.metrics.undefined["sortino"] == "no_downside"
    assert result.metrics.undefined["calmar"] == "too_short"
    assert result.metrics.max_drawdown == Decimal("0")
    assert result.metrics.net_return == Decimal("0.1")
    assert result.metrics.sharpe > 0
    assert result.trades[0].realized_pnl == Decimal("1000.00")


def test_an_open_position_is_marked_and_left_out_of_the_trade_stats() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": _eq("close", 100)}],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 110)]})
    _finite(result)
    assert result.metrics.closed_trades == 0
    assert result.metrics.open_trades == 1
    assert result.metrics.undefined["win_rate"] == "no_closed_trades"
    assert result.metrics.win_streak == 0
    assert result.metrics.loss_streak == 0
    assert result.metrics.net_return == Decimal("0.1")
    trade = result.trades[0]
    assert trade.status == "open"
    assert trade.exit_time is None
    assert trade.exit_price is None
    assert trade.realized_pnl == Decimal("0.00")
    assert trade.unrealized_pnl == Decimal("1000.00")
    row = json.loads(trade_log_json(result.trades))["trades"][0]
    assert row["status"] == "open"
    assert row["exit_time"] is None
    assert row["exit_price"] is None
    assert row["unrealized_pnl"] == "1000.00"


def test_a_scratch_breaks_streaks_and_is_not_a_win() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "stop", "expr": _eq("close", 100)},
        ],
        rules=[
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
        ],
        engine={
            "bar_size": "1m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 1,
        },
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 50), _bar(60, 100), _bar(120, 100)]})
    _finite(result)
    assert result.metrics.closed_trades == 1
    assert result.trades[0].realized_pnl == Decimal("0.00")
    assert result.metrics.win_rate == Decimal("0")
    assert result.metrics.undefined["payoff_ratio"] == "no_wins"
    assert result.metrics.win_streak == 0
    assert result.metrics.loss_streak == 0


def test_streaks_payoff_and_drawdown_match_the_hand_series() -> None:
    """Daily bars. Closes 100, 110, 90.

    Returns are 0, 1/10, and -2/11. Net return is -1/10.
    Max drawdown is 2000/11000 = 2/11 = 0.1818181818 at 10 places.
    One losing trade. Sharpe uses the sample standard deviation.
    """
    document = _strategy(
        engine={
            "bar_size": "1d",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "stop", "expr": _eq("close", 90)},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    day = 86_400
    result = run_backtest(
        document,
        {
            "sol": [
                _bar(0, 100, timeframe="1d"),
                _bar(day, 110, timeframe="1d"),
                _bar(2 * day, 90, timeframe="1d"),
            ]
        },
    )
    _finite(result)
    assert result.metrics.bars_per_year == 365
    assert result.metrics.net_return == Decimal("-0.1")
    assert result.metrics.max_drawdown == Decimal("0.1818181818")
    assert result.metrics.closed_trades == 1
    assert result.metrics.win_rate == Decimal("0")
    assert result.metrics.loss_streak == 1
    assert result.trades[0].realized_pnl == Decimal("-1000.00")
    returns = [Fraction(0), Fraction(1, 10), Fraction(-2, 11)]
    mean = sum(returns, Fraction(0)) / 3
    variance = sum((item - mean) ** 2 for item in returns) / 2
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        std = (Decimal(variance.numerator) / Decimal(variance.denominator)).sqrt()
        mean_dec = Decimal(mean.numerator) / Decimal(mean.denominator)
        sharpe = (mean_dec / std * Decimal(365).sqrt()).quantize(Decimal("1e-10"))
    assert result.metrics.sharpe == sharpe
    assert result.metrics.sharpe < 0
    downside = sum(min(item, Fraction(0)) ** 2 for item in returns) / 2
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        deviation = (Decimal(downside.numerator) / Decimal(downside.denominator)).sqrt()
        sortino = (mean_dec / deviation * Decimal(365).sqrt()).quantize(Decimal("1e-10"))
        grown = (Decimal("0.9") ** (Decimal(365) / Decimal(3))) - 1
        annualized = grown.quantize(Decimal("1e-10"))
        by_next = (Decimal("0.9") ** (Decimal(365) / Decimal(4))) - 1
    assert result.metrics.sortino == sortino
    assert result.metrics.undefined.get("sortino") is None
    assert result.metrics.annualized_return == annualized
    assert result.metrics.annualized_return != by_next.quantize(Decimal("1e-10"))
    assert result.metrics.calmar == (annualized / result.metrics.max_drawdown).quantize(
        Decimal("1e-10")
    )
    net_calmar = (result.metrics.net_return / result.metrics.max_drawdown).quantize(
        Decimal("1e-10")
    )
    assert result.metrics.calmar != net_calmar


def test_win_and_loss_streaks_follow_closed_trades_only() -> None:
    """Exit then enter, both always true. Closes 100, 110, 120, 100.

    Closed pnl is +1000, +1000, -2000. The last bar opens a new long, and
    that open trade does not extend the loss streak. Payoff is 1000/2000.
    Win rate is 2/3.
    """
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": {"op": "lit", "value": True}},
            {"id": "stop", "expr": {"op": "lit", "value": True}},
        ],
        rules=[
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
        ],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(
        document,
        {"sol": [_bar(0, 100), _bar(60, 110), _bar(120, 120), _bar(180, 100)]},
    )
    _finite(result)
    assert [trade.realized_pnl for trade in result.trades if trade.status == "closed"] == [
        Decimal("1000.00"),
        Decimal("1000.00"),
        Decimal("-2000.00"),
    ]
    assert result.metrics.open_trades == 1
    assert result.metrics.win_streak == 2
    assert result.metrics.loss_streak == 1
    assert result.metrics.win_rate == Decimal("0.6666666667")
    assert result.metrics.payoff_ratio == Decimal("0.5")


def test_a_zero_initial_cent_does_not_divide_by_zero() -> None:
    result = run_backtest(
        _strategy(capital={"initial": 0.001, "currency": "USDC"}),
        {"sol": [_bar(0, 1)]},
    )
    _finite(result)
    assert result.metrics.initial_equity == Decimal("0.00")
    assert result.metrics.undefined["net_return"] == "zero_initial"
    assert result.metrics.undefined["sharpe"] == "zero_initial"
    assert result.metrics.undefined["annualized_return"] == "zero_initial"
    assert result.metrics.undefined["calmar"] == "zero_initial"


def test_a_take_profit_gap_above_the_low_is_flagged() -> None:
    """The limit is 110.055. A low of 130 never traded there. A low of 100 did."""
    payload = json.loads(_OCO_FIXTURE.read_text(encoding="utf-8"))
    document = _strategy(
        costs=payload["costs"],
        signals=[{"id": "go", "expr": _eq("close", 100)}],
    )
    document["actions"][0]["risk"] = _risk(
        payload["take_profit_percent"], payload["stop_loss_percent"]
    )
    touched = run_backtest(
        document,
        {"sol": [_fixture_bar(payload["entry"]), _fixture_bar(payload["take_profit_bar"])]},
    )
    assert touched.fills[1].limit_below_low is False
    assert touched.metrics.take_profit_limit_below_low == 0
    assert touched.trades[0].limit_below_low is False

    gap = run_backtest(
        document,
        {
            "sol": [
                _fixture_bar(payload["entry"]),
                _bar(60, 140, open_=130, high=150, low=130),
            ]
        },
    )
    assert gap.fills[1].leg == "take_profit"
    assert gap.fills[1].price == _money(payload, "take_profit_fill")
    assert gap.fills[1].limit_below_low is True
    assert gap.metrics.take_profit_limit_below_low == 1
    assert gap.trades[0].limit_below_low is True
    assert json.loads(trade_log_json(gap.trades))["trades"][0]["limit_below_low"] == "true"


def test_suspect_outage_is_a_strictly_longer_quiet_run() -> None:
    document = _sma()
    quiet = [_bar(0, 10)] + [_bar(60 * (index + 1), 10, synthetic=True) for index in range(120)]
    under = run_backtest(document, {"sol": quiet})
    assert under.metrics.suspect_outage is False
    assert under.metrics.long_no_trade_bars == 0
    assert under.metrics.long_no_trade_threshold == 120
    assert not any("suspect_outage" in note for note in under.lints)

    longer = quiet + [_bar(60 * 121, 10, synthetic=True)]
    flagged = run_backtest(document, {"sol": longer})
    assert flagged.metrics.suspect_outage is True
    assert flagged.metrics.long_no_trade_bars == 121
    assert flagged.metrics.long_no_trade_minutes == 121
    assert flagged.metrics.suspect_outage_bars == 121
    assert any(
        note == "sma_2 on sol is computed across 121 suspect_outage bars"
        for note in flagged.lints
    )


def test_an_indicator_across_a_suspect_outage_is_linted_and_not_masked() -> None:
    """SMA(4) of 10, 10, 10, 40 is 17.5. Masking the stale 10s would make it empty.

    The three synthetic minutes are a suspect outage only when the cut is 2.
    A cut of 3 leaves them quiet and unlinted, and the signal still fires.
    """
    document = _sma(period=4, level=17)
    bars = [
        _bar(0, 1),
        _bar(60, 2),
        _bar(120, 3),
        _bar(180, 10, synthetic=True),
        _bar(240, 10, synthetic=True),
        _bar(300, 10, synthetic=True),
        _bar(360, 40),
    ]
    flagged = run_backtest(document, {"sol": bars}, long_no_trade_minutes=2)
    _finite(flagged)
    assert flagged.metrics.suspect_outage is True
    assert flagged.metrics.synthetic_bars == 3
    assert any(
        note == "sma_4 on sol is computed across 3 suspect_outage bars"
        for note in flagged.lints
    )
    assert [fill.open_time for fill in flagged.fills] == [_START + 360]

    quiet = run_backtest(document, {"sol": bars}, long_no_trade_minutes=3)
    assert quiet.metrics.suspect_outage is False
    assert not any("suspect_outage" in note for note in quiet.lints)
    assert [fill.open_time for fill in quiet.fills] == [_START + 360]


def test_outage_exposure_is_on_the_metrics_and_the_open_trade() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    bars = [_bar(0, 100)] + [_bar(60 * (index + 1), 100, synthetic=True) for index in range(5)]
    result = run_backtest(document, {"sol": bars}, long_no_trade_minutes=4)
    _finite(result)
    assert result.metrics.synthetic_bars == 5
    assert result.metrics.missing_bars == 0
    assert result.metrics.synthetic_bars_in_position == 5
    assert result.metrics.suspect_outage_bars_in_position == 5
    assert result.metrics.suspect_outage_minutes_in_position == 5
    assert result.metrics.long_no_trade_minutes_in_position == 5
    trade = result.trades[0]
    assert trade.status == "open"
    assert trade.overlaps_suspect_outage is True
    assert trade.synthetic_bars == 5
    assert trade.suspect_outage_bars == 5
    assert trade.suspect_outage_minutes == 5
    assert trade.missing_bars == 0


def test_a_one_minute_hole_is_missing_and_not_a_suspect_outage() -> None:
    result = run_backtest(_flat(), {"sol": [_bar(0, 1), _bar(120, 1)]})
    assert result.metrics.grid_bars == 3
    assert result.metrics.missing_bars == 1
    assert result.metrics.synthetic_bars == 0
    assert result.metrics.suspect_outage is False
    assert result.metrics.missing_bars_in_position == 0


def test_a_five_minute_hole_run_is_a_suspect_outage_without_an_indicator_lint() -> None:
    """25 missing 5m buckets are 125 minutes. SMA(2) never reads those holes.

    The finite windows are the two real bars on each side, so nothing is
    linted. The metrics still count the holes.
    """
    step = 300
    document = _strategy(
        engine={
            "bar_size": "5m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
        derived_series=[
            {
                "id": "sma_2",
                "kind": "indicator",
                "indicator": "sma",
                "source": "close",
                "params": {"period": 2},
            }
        ],
        signals=[{"id": "go", "expr": _gt("close", 10_000)}],
    )
    bars = [
        _bar(0, 10, timeframe="5m"),
        _bar(step, 10, timeframe="5m"),
        _bar(27 * step, 10, timeframe="5m"),
        _bar(28 * step, 10, timeframe="5m"),
    ]
    result = run_backtest(document, {"sol": bars})
    assert result.metrics.missing_bars == 25
    assert result.metrics.suspect_outage is True
    assert result.metrics.long_no_trade_minutes == 125
    assert not any("suspect_outage" in note for note in result.lints)
    assert result.fills == ()


def _sma(period: int = 2, level: int = 0) -> dict:
    return _strategy(
        derived_series=[
            {
                "id": f"sma_{period}",
                "kind": "indicator",
                "indicator": "sma",
                "source": "close",
                "params": {"period": period},
            }
        ],
        signals=[{"id": "go", "expr": _gt(f"sma_{period}", level)}],
    )


_TRADE_HEADER = (
    "trade_id,asset_id,status,entry_time,exit_time,qty,entry_price,exit_price,"
    "entry_notional,exit_notional,commission,realized_pnl,unrealized_pnl,"
    "limit_below_low,overlaps_suspect_outage,synthetic_bars,missing_bars,"
    "long_no_trade_bars,long_no_trade_minutes,suspect_outage_bars,suspect_outage_minutes"
)


def test_the_trade_log_column_order_is_fixed() -> None:
    assert TRADE_COLUMNS == tuple(_TRADE_HEADER.split(","))
    assert trade_log_csv(()).splitlines() == [_TRADE_HEADER]
    assert "\r" not in trade_log_csv(())


def test_metric_csv_and_json_agree_and_print_zero_in_full() -> None:
    result = run_backtest(_flat(), {"sol": [_bar(0, 1), _bar(60, 1), _bar(120, 1)]})
    payload = json.loads(metrics_json(result.metrics))
    row = next(csv.DictReader(io.StringIO(metrics_csv(result.metrics))))
    assert payload["schema"] == METRICS_SCHEMA
    assert list(row) == list(METRIC_COLUMNS)
    assert list(payload["values"]) == list(METRIC_COLUMNS)
    for column in METRIC_COLUMNS:
        value = payload["values"][column]
        if value is None:
            assert row[column] == ""
            assert column in payload["undefined"]
        else:
            assert row[column] == value
            assert "E" not in value
            assert "e" not in value
    assert payload["values"]["net_return"] == format(result.metrics.net_return, "f")
    assert payload["values"]["net_return"] != "0E-10"
    assert payload["undefined"]["sharpe"] == "zero_variance"


def test_logs_are_byte_identical_across_hash_seeds() -> None:
    script = """
from app.backtest import metrics_csv, metrics_json, run_backtest, trade_log_csv, trade_log_json
from app.ohlcv.bars import Bar

start = 1767225600
mint = "SyntheticMint111111111111111111111111111"
bars = [Bar(mint, "1m", start + 60 * i, 100, 100, 100, 100, 1, False) for i in range(3)]
document = {
    "ir_version": "1.0",
    "strategy": {"id": "hand", "name": "Hand"},
    "engine": {
        "bar_size": "1m", "event": "bar_close", "fill_price_mode": "on_close", "warmup_bars": 0
    },
    "capital": {"initial": 10000, "currency": "USDC"},
    "costs": {"commission_bps": 0, "slippage_bps": 0},
    "assets": [
        {"id": "sol", "symbol": "SOL", "chain": "solana", "market": "spot", "quote": "USDC"}
    ],
    "derived_series": [],
    "signals": [{"id": "go", "expr": {"op": "lit", "value": False}}],
    "actions": [{
        "id": "enter_sol", "type": "enter", "side": "long", "asset": "sol",
        "order": {"type": "market"}, "sizing": {"mode": "percent_equity", "percent": 100},
        "risk": {"type": "oco", "evaluation": "on_entry_fill", "movement": "tighten_only",
                 "take_profit": {"mode": "percent", "percent": 6},
                 "stop_loss": {"mode": "percent", "percent": 3}},
    }],
    "rules": [{"id": "enter_rule", "when": "go", "do": "enter_sol"}],
    "portfolio": {"mode": "spot", "direction": "long_only", "chain": "solana",
                  "max_positions": 1, "allow_pyramiding": False, "quote_currency": "USDC"},
}
result = run_backtest(document, {"sol": bars})
print(trade_log_csv(result.trades), end="")
print(trade_log_json(result.trades), end="")
print(metrics_csv(result.metrics), end="")
print(metrics_json(result.metrics), end="")
"""
    root = str(__file__).rsplit("/tests/", 1)[0]
    first = _run_seed(script, root, "1")
    second = _run_seed(script, root, "777")
    assert first == second
    assert "0E-10" not in first


def _run_seed(script: str, root: str, seed: str) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = root
    env["PYTHONHASHSEED"] = seed
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout


def test_case_g_does_not_crash_on_a_short_calmar() -> None:
    """60 one-minute bars, buy and hold, +0.95% with one 0.017 dip.

    Annualizing that hour overflows the 1e-10 quantize. The run is shorter
    than a day, so annualized return and Calmar are null, and the book still
    finishes.
    """
    bars = []
    for index in range(60):
        price = round(100 + (100.95 - 100) * index / 59, 6)
        if index == 30:
            price = round(price - 0.017, 6)
        bars.append(_bar(60 * index, price))
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    document["actions"][0]["risk"] = _risk(50, 50)
    result = run_backtest(document, {"sol": bars})
    _finite(result)
    assert result.metrics.bar_count == 60
    assert result.metrics.net_return is not None
    assert result.metrics.net_return > 0
    assert result.metrics.undefined["annualized_return"] == "too_short"
    assert result.metrics.undefined["calmar"] == "too_short"
    assert result.metrics.sharpe is not None


def test_annualization_past_one_day_that_will_not_fit_is_null() -> None:
    """(1 + 99) ^ (365 / 3) does not fit in a 1e-10 quantize at 50 digits."""
    metrics = compute_metrics(
        initial=Decimal("10000"),
        curve=(Decimal("10000"), Decimal("9000"), Decimal("1000000")),
        closed_pnls=(),
        open_trades=1,
        bar_seconds=86_400,
        take_profit_limit_below_low=0,
        quality=QualityCounts(3, 0, 0, 0, 0, 120, 0, 0, 0, 0),
    )
    assert metrics.undefined["annualized_return"] == "annualization_overflow"
    assert metrics.undefined["calmar"] == "annualization_overflow"
    assert metrics.net_return == Decimal("99")


def test_ratio_math_ignores_the_caller_precision() -> None:
    quality = QualityCounts(2, 0, 0, 0, 0, 120, 0, 0, 0, 0)
    kwargs = {
        "initial": Decimal("10000"),
        "curve": (Decimal("10000"), Decimal("11000")),
        "closed_pnls": (Decimal("1000"),),
        "open_trades": 0,
        "bar_seconds": 86_400,
        "take_profit_limit_below_low": 0,
        "quality": quality,
    }
    full = compute_metrics(**kwargs)
    with localcontext() as ctx:
        ctx.prec = 6
        narrow = compute_metrics(**kwargs)
    assert narrow.sharpe == full.sharpe
    assert narrow.annualized_return == full.annualized_return
    assert full.sharpe is not None


def test_a_flat_day_annualizes_to_zero_and_a_total_loss_to_minus_one() -> None:
    flat = run_backtest(
        _strategy(
            engine={
                "bar_size": "1d",
                "event": "bar_close",
                "fill_price_mode": "on_close",
                "warmup_bars": 0,
            },
            signals=[{"id": "go", "expr": _gt("close", 10_000)}],
        ),
        {"sol": [_bar(0, 1, timeframe="1d"), _bar(86_400, 1, timeframe="1d")]},
    )
    assert flat.metrics.net_return == Decimal("0")
    assert flat.metrics.annualized_return == Decimal("0")
    assert flat.metrics.undefined["calmar"] == "no_drawdown"

    wiped = _strategy(
        engine={
            "bar_size": "1d",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
        costs={"commission_bps": 10_000, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "stop", "expr": _eq("close", 90)},
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
        ],
    )
    wiped["actions"][0]["risk"] = _risk(100, 99)
    lost = run_backtest(
        wiped,
        {
            "sol": [
                _bar(0, 100, timeframe="1d"),
                _bar(86_400, 90, timeframe="1d"),
            ]
        },
    )
    assert lost.final_equity == Decimal("0.00")
    assert lost.metrics.net_return == Decimal("-1")
    assert lost.metrics.annualized_return == Decimal("-1")


def test_drawdown_and_net_return_start_from_initial_capital() -> None:
    """One buy with 10 bps. The only curve point is already below the capital.

    A peak that starts on that point reports no drawdown, and a net return
    measured from it is 0.
    """
    document = _strategy(
        costs={"commission_bps": 10, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 100)]})
    assert result.equity_curve[0].equity == Decimal("9990.01")
    assert result.metrics.net_return == Decimal("-0.000999")
    assert result.metrics.max_drawdown == Decimal("0.000999")


def test_a_scratch_between_wins_resets_the_streak() -> None:
    def run(prices: list[int]):
        document = _strategy(
            costs={"commission_bps": 0, "slippage_bps": 0},
            signals=[
                {"id": "go", "expr": {"op": "lit", "value": True}},
                {"id": "stop", "expr": {"op": "lit", "value": True}},
            ],
            rules=[
                {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
                {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            ],
        )
        document["actions"][0]["risk"] = _risk(100, 99)
        return run_backtest(
            document, {"sol": [_bar(60 * index, price) for index, price in enumerate(prices)]}
        )

    scratch = run([100, 110, 110, 120])
    assert [trade.realized_pnl for trade in scratch.trades if trade.status == "closed"] == [
        Decimal("1000.00"),
        Decimal("0.00"),
        Decimal("1000.00"),
    ]
    assert scratch.metrics.win_streak == 1
    loss = run([100, 110, 100, 110])
    assert [trade.realized_pnl for trade in loss.trades if trade.status == "closed"] == [
        Decimal("1000.00"),
        Decimal("-1000.00"),
        Decimal("1000.00"),
    ]
    assert loss.metrics.win_streak == 1
    assert loss.metrics.loss_streak == 1


def test_closed_trades_follow_exit_order_not_entry_order() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
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
        signals=[
            {"id": "go_sol", "expr": _eq("close", 100)},
            {"id": "go_jup", "expr": _eq("close", 100)},
            {"id": "out_jup", "expr": _eq("close", 120)},
            {"id": "out_sol", "expr": _eq("close", 80)},
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
                "id": "enter_jup",
                "type": "enter",
                "side": "long",
                "asset": "jup",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            },
            {
                "id": "exit_jup",
                "type": "exit",
                "side": "long",
                "asset": "jup",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
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
            {"id": "enter_sol_rule", "when": "go_sol", "do": "enter_sol"},
            {"id": "enter_jup_rule", "when": "go_jup", "do": "enter_jup"},
            {"id": "exit_jup_rule", "when": "out_jup", "do": "exit_jup"},
            {"id": "exit_sol_rule", "when": "out_sol", "do": "exit_sol"},
        ],
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 2,
            "allow_pyramiding": False,
            "quote_currency": "USDC",
        },
    )
    result = run_backtest(
        document,
        {
            "sol": [_bar(0, 100), _bar(60, 101), _bar(120, 102), _bar(180, 80)],
            "jup": [_bar(0, 50), _bar(60, 100), _bar(120, 120), _bar(180, 90)],
        },
    )
    closed = [trade for trade in result.trades if trade.status == "closed"]
    assert [trade.asset_id for trade in closed] == ["jup", "sol"]
    assert closed[0].realized_pnl == Decimal("1000.00")
    assert closed[1].realized_pnl == Decimal("-1000.00")
    assert [trade.exit_time for trade in closed] == [_START + 120, _START + 180]


def test_a_take_profit_at_the_low_is_not_below_it() -> None:
    """The raw limit is 110, equal to the low. The open is 112, above it."""
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": _eq("close", 100)}],
    )
    document["actions"][0]["risk"] = _risk(10, 5)
    result = run_backtest(
        document,
        {
            "sol": [
                _bar(0, 100),
                _bar(60, 112, open_=112, high=115, low=110),
            ]
        },
    )
    assert result.fills[1].leg == "take_profit"
    assert result.fills[1].price == Decimal("110")
    assert result.fills[1].limit_below_low is False
    assert result.metrics.take_profit_limit_below_low == 0
    assert result.trades[0].limit_below_low is False


def test_a_half_equity_budget_rounds_half_even() -> None:
    """3333.35 * 50% = 1666.675. The kept digit is odd, so the cent is 1666.68."""
    document = _strategy(
        capital={"initial": 3333.35, "currency": "USDC"},
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
    result = run_backtest(document, {"sol": [_bar(0, 10)]})
    assert result.fills[0].notional == Decimal("1666.68")


def test_open_equity_uses_the_exact_quantity() -> None:
    """The open long is marked with the grid quantity, so the cent is 906.67."""
    document = _strategy(
        capital={"initial": 43175, "currency": "USDC"},
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": _gt("close", 10)}],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 78.834), _bar(60, 1.655514)]})
    assert result.final_positions
    assert result.final_equity == Decimal("906.67")


def test_buy_commission_is_in_the_open_unrealized_pnl() -> None:
    document = _strategy(
        costs={"commission_bps": 10, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 100)]})
    assert result.trades[0].unrealized_pnl == Decimal("-9.99")
    assert result.final_equity - result.metrics.initial_equity == Decimal("-9.99")


def test_a_later_add_does_not_reprice_a_partial_sale() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "trim", "expr": _eq("close", 110)},
            {"id": "add", "expr": _eq("close", 80)},
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
                "id": "add_sol",
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
                "sizing": {"mode": "percent_position", "percent": 50},
            },
        ],
        rules=[
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
            {"id": "exit_rule", "when": "trim", "do": "exit_sol"},
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
    result = run_backtest(
        document,
        {"sol": [_bar(0, 100), _bar(60, 110), _bar(120, 80)]},
    )
    assert result.trades[0].realized_pnl == Decimal("500.00")
    assert result.trades[0].status == "open"
    total = result.trades[0].realized_pnl + result.trades[0].unrealized_pnl
    assert total == result.final_equity - result.metrics.initial_equity


def test_realized_plus_unrealized_matches_the_equity_change() -> None:
    """Seeded books with adds, partial sells, and commission.

    The replay is the exact basis, before cent rounding. The reported cents
    may differ from that replay by at most one cent on each open trade.
    """
    for seed in (617, *range(30)):
        result, bars = _random_book(seed)
        _assert_exact_pnl_and_plug(result, bars)


def _random_book(seed: int):
    rng = random.Random(seed)
    commission = rng.choice((0, 5, 10, 25))
    document = _strategy(
        costs={"commission_bps": commission, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _gt("close", 100)},
            {
                "id": "trim",
                "expr": {
                    "op": "and",
                    "left": {
                        "op": "lt",
                        "left": {"op": "ref", "name": "close"},
                        "right": {"op": "lit", "value": 90},
                    },
                    "right": {
                        "op": "gte",
                        "left": {"op": "ref", "name": "close"},
                        "right": {"op": "lit", "value": 80},
                    },
                },
            },
            {
                "id": "flat",
                "expr": {
                    "op": "lt",
                    "left": {"op": "ref", "name": "close"},
                    "right": {"op": "lit", "value": 80},
                },
            },
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 40},
                "risk": _risk(100, 99),
            },
            {
                "id": "exit_half",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 50},
            },
            {
                "id": "exit_all",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        rules=[
            {"id": "flat_rule", "when": "flat", "do": "exit_all"},
            {"id": "trim_rule", "when": "trim", "do": "exit_half"},
            {"id": "enter_rule", "when": "go", "do": "enter_sol"},
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
    bars = []
    for index in range(rng.randint(6, 12)):
        kind = rng.randrange(4)
        if kind == 0:
            price = rng.uniform(101, 130)
        elif kind == 1:
            price = rng.uniform(80, 89.9)
        elif kind == 2:
            price = rng.uniform(70, 79.9)
        else:
            price = rng.uniform(90, 100)
        bars.append(_bar(60 * index, round(price, 4)))
    return run_backtest(document, {"sol": bars}), bars


def test_a_long_one_minute_hole_is_not_quiet() -> None:
    result = run_backtest(_flat(), {"sol": [_bar(0, 1), _bar(122 * 60, 1)]})
    assert result.metrics.missing_bars == 121
    assert result.metrics.suspect_outage is False


def test_leading_holes_before_the_first_real_bar_are_not_quiet() -> None:
    step = 300
    count = 30
    document = _strategy(
        engine={
            "bar_size": "5m",
            "event": "bar_close",
            "fill_price_mode": "on_close",
            "warmup_bars": 0,
        },
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
        signals=[{"id": "go", "expr": _gt("close", 10_000)}],
    )
    result = run_backtest(
        document,
        {
            "sol": [_bar(index * step, 10, timeframe="5m") for index in range(count)],
            "jup": [_bar((count - 1) * step, 10, timeframe="5m")],
        },
    )
    assert result.metrics.missing_bars == count - 1
    assert result.metrics.suspect_outage is False


def test_an_sma_window_counts_only_the_outage_bars_it_reads() -> None:
    bars = [
        _bar(0, 10, synthetic=True),
        _bar(60, 10, synthetic=True),
        _bar(120, 11),
        _bar(180, 11),
        _bar(240, 11),
        _bar(300, 11),
    ]
    result = run_backtest(_sma(period=4, level=10_000), {"sol": bars}, long_no_trade_minutes=1)
    assert any(
        note == "sma_4 on sol is computed across 2 suspect_outage bars" for note in result.lints
    )


def test_a_seeded_run_without_a_finite_output_is_not_linted() -> None:
    document = _strategy(
        derived_series=[
            {
                "id": "ema_10",
                "kind": "indicator",
                "indicator": "ema",
                "source": "close",
                "params": {"period": 10},
            }
        ],
        signals=[{"id": "go", "expr": _gt("close", 10_000)}],
    )
    bars = [_bar(0, 10, synthetic=True), _bar(60, 10, synthetic=True)]
    result = run_backtest(document, {"sol": bars}, long_no_trade_minutes=1)
    assert result.metrics.suspect_outage is True
    assert not any("suspect_outage" in note for note in result.lints)


def test_stochastic_lookback_reaches_the_full_k_and_d_window() -> None:
    document = _strategy(
        derived_series=[
            {
                "id": "stoch_d",
                "kind": "indicator",
                "indicator": "stochastic",
                "source": "close",
                "output": "d",
                "params": {"k_period": 3, "d_period": 3},
            }
        ],
        signals=[{"id": "go", "expr": _gt("close", 10_000)}],
    )
    bars = [
        _bar(0, 10, synthetic=True),
        _bar(60, 10, synthetic=True),
    ]
    for index, close in enumerate((11, 12, 13, 14, 15), start=2):
        bars.append(_bar(60 * index, close, high=close + 2, low=close - 2))
    result = run_backtest(document, {"sol": bars}, long_no_trade_minutes=1)
    assert any(
        note == "stoch_d on sol is computed across 2 suspect_outage bars"
        for note in result.lints
    )


def test_synthetic_bars_do_not_set_the_outage_flag_on_their_own() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    bars = [_bar(0, 100), _bar(60, 100, synthetic=True), _bar(120, 100, synthetic=True)]
    result = run_backtest(document, {"sol": bars})
    trade = result.trades[0]
    assert trade.synthetic_bars == 2
    assert trade.overlaps_suspect_outage is False


def _assert_exact_pnl_and_plug(result, bars: list[Bar]) -> None:
    """Replay each fill's basis. Slippage on these books is zero.

    Buy quantity is the grid quantity on the fill. A sell carries the quantity
    it closed. The exact sum has to match unrounded equity before any cent plug.
    """
    state = {"qty": Fraction(0), "basis": Fraction(0), "realized": Fraction(0)}
    closed_exact: list[Fraction] = []
    for fill in result.fills:
        if fill.side == "buy":
            state["basis"] += Fraction(fill.notional) + Fraction(fill.commission)
            state["qty"] += Fraction(fill.qty)
            continue
        qty = state["qty"]
        sold = Fraction(fill.qty)
        if sold <= 0 or sold > qty:
            raise AssertionError("sell qty is outside the open quantity")
        sold_basis = state["basis"] * sold / qty
        state["realized"] += Fraction(fill.notional) - Fraction(fill.commission) - sold_basis
        state["basis"] -= sold_basis
        state["qty"] = qty - sold
        if state["qty"] == 0:
            closed_exact.append(state["realized"])
            state["basis"] = Fraction(0)
            state["realized"] = Fraction(0)
    mark = Decimal(str(bars[-1].close))
    exact = sum(closed_exact, Fraction(0)) + state["realized"]
    if state["qty"] != 0:
        exact += state["qty"] * Fraction(mark) - state["basis"]
    exact_equity = Fraction(result.final_cash)
    if state["qty"] != 0:
        exact_equity += state["qty"] * Fraction(mark)
    initial = Fraction(result.metrics.initial_equity)
    assert exact == exact_equity - initial
    reported_closed = [trade.realized_pnl for trade in result.trades if trade.status == "closed"]
    assert reported_closed == [quantize_cents(item) for item in closed_exact]
    open_trades = [trade for trade in result.trades if trade.status == "open"]
    if state["qty"] == 0:
        assert open_trades == []
    else:
        assert len(open_trades) == 1
        trade = open_trades[0]
        assert trade.realized_pnl == quantize_cents(state["realized"])
        plug = trade.unrealized_pnl - quantize_cents(
            state["qty"] * Fraction(mark) - state["basis"]
        )
        assert abs(plug) <= Decimal("0.01")
    reported = sum(
        (trade.realized_pnl + trade.unrealized_pnl for trade in result.trades), Decimal("0.00")
    )
    assert reported == result.final_equity - result.metrics.initial_equity


def test_a_full_day_calmar_overflow_does_not_fail_the_run() -> None:
    """1440 one-minute bars, buy and hold, +25% with a 0.018 dip at bar 700.

    The run is one day, so it is annualized. Calmar is past the 1e-10
    quantize. Removing the overflow guard fails this run. Case G never
    reaches that guard, because an hour is too_short.
    """
    bars = []
    for index in range(1440):
        price = round(100 + 25 * index / 1439, 6)
        if index == 700:
            price = round(price - 0.018, 6)
        bars.append(_bar(60 * index, price))
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    document["actions"][0]["risk"] = _risk(50, 50)
    result = run_backtest(document, {"sol": bars})
    _finite(result)
    assert result.metrics.bar_count == 1440
    assert result.metrics.net_return == Decimal("0.25")
    assert result.metrics.annualized_return is not None
    assert result.metrics.calmar is None
    assert result.metrics.undefined["calmar"] == "annualization_overflow"


def test_one_day_is_annualized_and_the_bar_before_is_too_short() -> None:
    def flat(count: int):
        return run_backtest(_flat(), {"sol": [_bar(60 * index, 1) for index in range(count)]})

    full = flat(1440)
    short = flat(1439)
    assert full.metrics.bar_count == 1440
    assert full.metrics.annualized_return == Decimal("0")
    assert "annualized_return" not in full.metrics.undefined
    assert full.metrics.undefined["calmar"] == "no_drawdown"
    assert short.metrics.bar_count == 1439
    assert short.metrics.net_return == Decimal("0")
    assert short.metrics.annualized_return is None
    assert short.metrics.undefined["annualized_return"] == "too_short"
    assert short.metrics.undefined["calmar"] == "too_short"


def test_fewer_than_two_returns_are_null() -> None:
    metrics = compute_metrics(
        initial=Decimal("10000"),
        curve=(Decimal("11000"),),
        closed_pnls=(),
        open_trades=1,
        bar_seconds=60,
        take_profit_limit_below_low=0,
        quality=QualityCounts(1, 0, 0, 0, 0, 120, 0, 0, 0, 0),
    )
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.undefined["sharpe"] == "too_few_returns"
    assert metrics.undefined["sortino"] == "too_few_returns"
    assert metrics.net_return == Decimal("0.1")


def test_a_payoff_that_will_not_quantize_is_overflow() -> None:
    metrics = compute_metrics(
        initial=Decimal("10000"),
        curve=(Decimal("10000"), Decimal("10000")),
        closed_pnls=(Decimal("1E+60"), Decimal("-0.01")),
        open_trades=0,
        bar_seconds=86_400,
        take_profit_limit_below_low=0,
        quality=QualityCounts(2, 0, 0, 0, 0, 120, 0, 0, 0, 0),
    )
    assert metrics.payoff_ratio is None
    assert metrics.undefined["payoff_ratio"] == "overflow"
    assert metrics.annualized_return == Decimal("0")


def test_a_caller_inexact_trap_does_not_blank_ratios() -> None:
    quality = QualityCounts(2, 0, 0, 0, 0, 120, 0, 0, 0, 0)
    kwargs = {
        "initial": Decimal("10000"),
        "curve": (Decimal("10000"), Decimal("11000")),
        "closed_pnls": (Decimal("1000"),),
        "open_trades": 0,
        "bar_seconds": 86_400,
        "take_profit_limit_below_low": 0,
        "quality": quality,
    }
    plain = compute_metrics(**kwargs)
    with localcontext() as ctx:
        ctx.traps[Inexact] = True
        trapped = compute_metrics(**kwargs)
    assert plain.sharpe is not None
    assert trapped.sharpe == plain.sharpe
    assert trapped.net_return == plain.net_return
    assert trapped.sortino == plain.sortino
    assert trapped.annualized_return == plain.annualized_return
    assert trapped.undefined == plain.undefined


def test_a_sub_cent_pnl_error_fails_the_run() -> None:
    import app.backtest.engine as engine

    real = engine._book_pnl

    def planted(*args, **kwargs):
        return real(*args, **kwargs) + Fraction(4, 1000)

    engine._book_pnl = planted
    try:
        try:
            run_backtest(_flat(), {"sol": [_bar(0, 1), _bar(60, 1)]})
        except BacktestError as exc:
            assert "do not match the equity change" in str(exc)
        else:
            raise AssertionError("a 0.4 cent pnl error was absorbed")
    finally:
        engine._book_pnl = real


def test_the_cent_remainder_goes_to_the_larger_unrealized_remainder() -> None:
    """Two open trades, one cent short. The earlier, larger remainder gets it."""

    def trade(asset: str, trade_id: int) -> Trade:
        return Trade(
            trade_id=trade_id,
            asset_id=asset,
            status="open",
            entry_time=0,
            exit_time=None,
            qty=Decimal("1"),
            entry_price=Decimal("1"),
            exit_price=None,
            entry_notional=Decimal("1.00"),
            exit_notional=Decimal("0.00"),
            commission=Decimal("0.00"),
            realized_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("1.00"),
            limit_below_low=False,
            overlaps_suspect_outage=False,
            synthetic_bars=0,
            missing_bars=0,
            long_no_trade_bars=0,
            long_no_trade_minutes=0,
            suspect_outage_bars=0,
            suspect_outage_minutes=0,
        )

    trades = (trade("sol", 1), trade("jup", 2))
    # Reported cents sum to 2.00. The target is 2.01, so one cent is added.
    # sol was rounded down by 0.004, jup by 0.001. sol receives the cent.
    spread = _spread_remainder(
        trades, (Fraction("1.004"), Fraction("1.001")), Decimal("0"), Decimal("2.01")
    )
    assert spread[0].unrealized_pnl == Decimal("1.01")
    assert spread[1].unrealized_pnl == Decimal("1.00")
    tied = _spread_remainder(
        trades, (Fraction("1.004"), Fraction("1.004")), Decimal("0"), Decimal("2.01")
    )
    assert tied[0].unrealized_pnl == Decimal("1.01")
    assert tied[1].unrealized_pnl == Decimal("1.00")
    # A cent too high comes off the trade that was rounded up, even when it is first.
    over = _spread_remainder(
        trades, (Fraction("0.996"), Fraction("1.001")), Decimal("0"), Decimal("1.99")
    )
    assert over[0].unrealized_pnl == Decimal("0.99")
    assert over[1].unrealized_pnl == Decimal("1.00")


def test_a_partial_sale_keeps_the_exact_basis_split() -> None:
    """50% of a 10.01 basis is 5.005. Realized is the cent of 10.01 - 5.005.

    Rounding the basis share to a cent first would report 5.01.
    """
    document = _strategy(
        capital={"initial": 10.01, "currency": "USDC"},
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 1)},
            {"id": "trim", "expr": _eq("close", 1.5)},
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
            {"id": "exit_rule", "when": "trim", "do": "exit_sol"},
        ],
    )
    result = run_backtest(document, {"sol": [_bar(0, 1), _bar(60, 1.5)]})
    trade = result.trades[0]
    assert trade.status == "open"
    assert trade.realized_pnl == Decimal("2.50")
    assert trade.realized_pnl + trade.unrealized_pnl == result.final_equity - Decimal("10.01")


def test_a_scratch_is_not_a_payoff_loss() -> None:
    metrics = compute_metrics(
        initial=Decimal("10000"),
        curve=(Decimal("10000"), Decimal("10100"), Decimal("10050")),
        closed_pnls=(Decimal("100.00"), Decimal("0.00"), Decimal("-50.00")),
        open_trades=0,
        bar_seconds=86_400,
        take_profit_limit_below_low=0,
        quality=QualityCounts(3, 0, 0, 0, 0, 120, 0, 0, 0, 0),
    )
    assert metrics.payoff_ratio == Decimal("2")
    assert "payoff_ratio" not in metrics.undefined


def test_price_cells_do_not_use_scientific_notation() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
    )
    document["actions"][0]["risk"] = _risk(100, 99)
    result = run_backtest(document, {"sol": [_bar(0, 1e-9), _bar(60, 1e-9)]})
    row = json.loads(trade_log_json(result.trades))["trades"][0]
    for column in ("qty", "entry_price"):
        assert "E" not in row[column]
        assert "e" not in row[column]
        assert row[column] == format(getattr(result.trades[0], column), "f")
    assert "E" not in trade_log_csv(result.trades)


def test_the_trade_log_schema_string_is_the_documented_id() -> None:
    payload = json.loads(trade_log_json(()))
    assert payload["schema"] == "tradoortest.trade_log.v1"
    assert SCHEMA == "tradoortest.trade_log.v1"


def test_entry_price_is_the_quantity_weighted_average() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "add", "expr": _eq("close", 150)},
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
                "sizing": {"mode": "percent_equity", "percent": 50},
                "risk": _risk(100, 99),
            },
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
    result = run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 150)]})
    trade = result.trades[0]
    # 50 at 100, then the cash cap of 5000 at 150 rounded down to 1e-12.
    first, second = (fill.qty for fill in result.fills)
    assert trade.status == "open"
    assert [fill.price for fill in result.fills] == [Decimal("100"), Decimal("150")]
    assert first == Decimal("50")
    assert second == floor_qty(Fraction(5000) / Fraction(150))
    assert trade.qty == first + second
    assert trade.entry_price == decimal_ratio(
        (Fraction(first) * 100 + Fraction(second) * 150) / Fraction(trade.qty)
    )


def test_closed_trades_are_listed_before_open_trades() -> None:
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
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
        signals=[
            {"id": "go_jup", "expr": _eq("close", 100)},
            {"id": "go_sol", "expr": _eq("close", 100)},
            {"id": "out_sol", "expr": _eq("close", 110)},
        ],
        actions=[
            {
                "id": "enter_jup",
                "type": "enter",
                "side": "long",
                "asset": "jup",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 40},
                "risk": _risk(100, 99),
            },
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 40},
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
            {"id": "enter_jup_rule", "when": "go_jup", "do": "enter_jup"},
            {"id": "enter_sol_rule", "when": "go_sol", "do": "enter_sol"},
            {"id": "exit_sol_rule", "when": "out_sol", "do": "exit_sol"},
        ],
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 2,
            "allow_pyramiding": False,
            "quote_currency": "USDC",
        },
    )
    result = run_backtest(
        document,
        {
            "jup": [_bar(0, 100), _bar(60, 101), _bar(120, 102)],
            "sol": [_bar(0, 50), _bar(60, 100), _bar(120, 110)],
        },
    )
    assert [trade.asset_id for trade in result.trades] == ["sol", "jup"]
    assert [trade.status for trade in result.trades] == ["closed", "open"]
    assert result.trades[1].entry_time < result.trades[0].entry_time


def test_a_seeded_ema_and_rsi_across_an_outage_are_linted() -> None:
    document = _strategy(
        derived_series=[
            {
                "id": "ema_3",
                "kind": "indicator",
                "indicator": "ema",
                "source": "close",
                "params": {"period": 3},
            },
            {
                "id": "rsi_2",
                "kind": "indicator",
                "indicator": "rsi",
                "source": "close",
                "params": {"period": 2},
            },
        ],
        signals=[{"id": "go", "expr": _gt("close", 10_000)}],
    )
    bars = [
        _bar(0, 10, synthetic=True),
        _bar(60, 11, synthetic=True),
        _bar(120, 12),
        _bar(180, 13),
        _bar(240, 14),
        _bar(300, 15),
    ]
    result = run_backtest(document, {"sol": bars}, long_no_trade_minutes=1)
    assert result.metrics.suspect_outage is True
    assert "ema_3 on sol is computed across 2 suspect_outage bars" in result.lints
    assert "rsi_2 on sol is computed across 2 suspect_outage bars" in result.lints


def _quality(bars: int) -> QualityCounts:
    return QualityCounts(bars, 0, 0, 0, 0, 120, 0, 0, 0, 0)


def _no_nan(metrics) -> None:
    text = metrics_json(metrics)
    assert "NaN" not in text
    assert "Infinity" not in text
    for name in _RATIO_NAMES:
        value = getattr(metrics, name)
        if value is not None:
            assert value.is_finite()
            assert not value.is_nan()


def test_a_caller_context_cannot_put_nan_in_a_ratio() -> None:
    """InvalidOperation off used to quantize an overflow into Decimal('NaN').

    Emax 30 used to null a finite annualized return, because the ratio context
    copied the caller's exponent range.
    """
    overflow = {
        "initial": Decimal("1"),
        "curve": (Decimal("1E+45"),),
        "closed_pnls": (),
        "open_trades": 0,
        "bar_seconds": 86_400,
        "take_profit_limit_below_low": 0,
        "quality": _quality(1),
    }
    plain_overflow = compute_metrics(**overflow)
    with localcontext() as ctx:
        ctx.traps[InvalidOperation] = False
        trapped = compute_metrics(**overflow)
    assert plain_overflow.net_return is None
    assert trapped.net_return is None
    assert trapped.undefined["net_return"] == "overflow"
    assert trapped.undefined["annualized_return"] == "overflow"
    _no_nan(trapped)

    normal = {
        "initial": Decimal("10000"),
        "curve": (Decimal("10000"), Decimal("11000")),
        "closed_pnls": (Decimal("1000"),),
        "open_trades": 0,
        "bar_seconds": 86_400,
        "take_profit_limit_below_low": 0,
        "quality": _quality(2),
    }
    grown = {
        "initial": Decimal("10000"),
        "curve": (Decimal("12500"),),
        "closed_pnls": (),
        "open_trades": 1,
        "bar_seconds": 86_400,
        "take_profit_limit_below_low": 0,
        "quality": _quality(1),
    }
    base_normal = compute_metrics(**normal)
    base_grown = compute_metrics(**grown)
    assert base_grown.annualized_return is not None
    with localcontext() as ctx:
        ctx.traps[InvalidOperation] = False
        ctx.Emax = 30
        trapped_normal = compute_metrics(**normal)
        trapped_grown = compute_metrics(**grown)
    assert trapped_normal.sharpe == base_normal.sharpe
    assert trapped_normal.annualized_return == base_normal.annualized_return
    assert trapped_grown.annualized_return == base_grown.annualized_return
    assert trapped_grown.annualized_return.is_finite()
    assert trapped.calmar is None
    assert trapped.undefined["calmar"] == "overflow"
    payoff = {
        "initial": Decimal("10000"),
        "curve": (Decimal("10000"), Decimal("10000")),
        "closed_pnls": (Decimal("1E+60"), Decimal("-1E-20")),
        "open_trades": 0,
        "bar_seconds": 60,
        "take_profit_limit_below_low": 0,
        "quality": _quality(2),
    }
    with localcontext() as ctx:
        ctx.traps[InvalidOperation] = False
        trapped_payoff = compute_metrics(**payoff)
    assert trapped_payoff.payoff_ratio is None
    assert trapped_payoff.undefined["payoff_ratio"] == "overflow"
    _no_nan(trapped)
    _no_nan(trapped_normal)
    _no_nan(trapped_grown)
    _no_nan(trapped_payoff)


def test_metrics_on_a_random_walk_stay_within_three_seconds() -> None:
    """A seeded cent walk. Seven repeated values hid the old exact-sum cost."""
    rng = random.Random(17)
    price = Decimal("10000.00")
    curve: list[Decimal] = []
    for _ in range(20_000):
        shock = Decimal(str(rng.gauss(0, 0.001)))
        price = (price * (1 + shock)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        if price < Decimal("0.01"):
            price = Decimal("0.01")
        curve.append(price)
    started = time.perf_counter()
    metrics = compute_metrics(
        initial=Decimal("10000.00"),
        curve=tuple(curve),
        closed_pnls=(),
        open_trades=0,
        bar_seconds=60,
        take_profit_limit_below_low=0,
        quality=_quality(20_000),
    )
    elapsed = time.perf_counter() - started
    # Headroom for a slow CI runner. Head is under a second. Three seconds still
    # fails a Fraction running sum, which spends the walk in gcd.
    assert elapsed < 3
    assert metrics.bar_count == 20_000
    assert metrics.sharpe is not None
    assert metrics.sharpe.is_finite()


def _pyramid(count: int) -> tuple[dict, dict[str, list]]:
    """One asset adds every bar and trims on four of every ten. The other round-trips."""
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
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
        signals=[
            {"id": "add", "expr": {"op": "lit", "value": True}},
            {"id": "trim", "expr": _gt("close", 105)},
            {"id": "enter_jup", "expr": _gt("close", 100)},
            {"id": "exit_jup", "expr": _eq("close", 90)},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 0.05},
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
            {
                "id": "enter_jup",
                "type": "enter",
                "side": "long",
                "asset": "jup",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 20},
                "risk": _risk(2, 2),
            },
            {
                "id": "exit_jup",
                "type": "exit",
                "side": "long",
                "asset": "jup",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 100},
            },
        ],
        rules=[
            {"id": "add_sol", "when": "add", "do": "enter_sol"},
            {"id": "trim_rule", "when": "trim", "do": "trim_sol"},
            {"id": "open_jup", "when": "enter_jup", "do": "enter_jup"},
            {"id": "flat_jup", "when": "exit_jup", "do": "exit_jup"},
        ],
        portfolio={
            "mode": "spot",
            "direction": "long_only",
            "chain": "solana",
            "max_positions": 2,
            "allow_pyramiding": True,
            "quote_currency": "USDC",
        },
    )
    sol = []
    jup = []
    for index in range(count):
        # Adds every bar. The close is above 105 on four of every ten bars,
        # and the position stays open for the whole run.
        sol_close = 100 + (index % 10)
        sol.append(_bar(60 * index, sol_close))
        jup_close = 110 if index % 2 == 0 else 90
        jup.append(_bar(60 * index, jup_close, mint="SyntheticMint222222222222222222222222222"))
    return document, {"sol": sol, "jup": jup}


def test_a_long_pyramid_and_many_trades_stay_within_eight_seconds() -> None:
    """20k bars. Eight seconds still fails a full-grid rescan of each trade.

    The slot counter is the clock-free twin: a bisect probes a few stamps per
    trade, and a walk of the whole grid touches one slot per bar per call.
    """
    import app.backtest.engine as backtest_engine

    document, bars = _pyramid(20_000)
    backtest_engine.between_slots = 0
    started = time.perf_counter()
    result = run_backtest(document, bars)
    elapsed = time.perf_counter() - started
    assert elapsed < 8
    # 20k bars, about 10k trades, two searches, ~15 probes each: under 2e6.
    # A full-grid rescan touches hundreds of millions of slots.
    assert 0 < backtest_engine.between_slots < 20_000 * 100
    assert len(result.trades) > 500
    assert sum(fill.side == "buy" and fill.asset_id == "sol" for fill in result.fills) > 300


def test_a_pyramid_from_5k_to_20k_bars_takes_under_eight_times_as_long() -> None:
    """Linear work grows about four times. A quadratic rescan grows about sixteen."""
    document, bars = _pyramid(5_000)
    started = time.perf_counter()
    run_backtest(document, bars)
    short = time.perf_counter() - started
    document, bars = _pyramid(20_000)
    started = time.perf_counter()
    run_backtest(document, bars)
    long = time.perf_counter() - started
    assert short > 0
    assert long / short < 8


def _dca_trim(count: int, *, close_out: bool = False) -> tuple[dict, list[Bar]]:
    """Buy 1% of equity on every bar at 100 or 110. Trim 33.3% when the close is 110.

    Index 0, 7, 14, … is the trim bar. A closing bar at 50 sells the rest.
    """
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "add", "expr": _gt("close", 90)},
            {"id": "trim", "expr": _eq("close", 110)},
            {"id": "flat", "expr": _eq("close", 50)},
        ],
        actions=[
            {
                "id": "enter_sol",
                "type": "enter",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_equity", "percent": 1},
                "risk": _risk(100, 99),
            },
            {
                "id": "trim_sol",
                "type": "exit",
                "side": "long",
                "asset": "sol",
                "order": {"type": "market"},
                "sizing": {"mode": "percent_position", "percent": 33.3},
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
            {"id": "flat_rule", "when": "flat", "do": "exit_sol"},
            {"id": "trim_rule", "when": "trim", "do": "trim_sol"},
            {"id": "add_rule", "when": "add", "do": "enter_sol"},
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
    bars = [_bar(60 * index, 110 if index % 7 == 0 else 100) for index in range(count)]
    if close_out:
        bars.append(_bar(60 * count, 50))
    return document, bars


def test_a_partial_sale_keeps_basis_on_a_fixed_grid() -> None:
    """33.3% trims. An exact split is past 30 denominator digits by the second sale."""
    import app.backtest.engine as engine

    seen: list[Fraction] = []
    real = engine._note_sell

    def record(*args, **kwargs):
        real(*args, **kwargs)
        asset_id = args[2]
        acc = args[0].get(asset_id)
        if acc is None:
            acc = args[1][-1]
        seen.append(acc.basis)
        seen.append(acc.realized_exact)

    engine._note_sell = record
    try:
        document, bars = _dca_trim(22, close_out=True)
        result = run_backtest(document, {"sol": bars})
    finally:
        engine._note_sell = real
    assert len(seen) >= 6
    limit = 10**30
    for amount in seen:
        assert amount.denominator <= limit
    assert result.final_positions == ()
    reported = sum(
        (trade.realized_pnl + trade.unrealized_pnl for trade in result.trades),
        Decimal("0.00"),
    )
    assert reported == result.final_equity - result.metrics.initial_equity


def test_repeated_one_third_trims_stay_within_eight_seconds() -> None:
    """Buy 1% every bar and trim 33.3% every seventh bar, for 20k bars.

    A 50% trim only halves the basis. This fraction does not terminate, and
    an exact split is already many seconds at this length. Eight seconds
    still fails that exact split. The 1e-30 pin below is the clock-free twin.
    """
    document, bars = _dca_trim(20_000)
    started = time.perf_counter()
    result = run_backtest(document, {"sol": bars})
    elapsed = time.perf_counter() - started
    assert elapsed < 8
    sells = [fill for fill in result.fills if fill.side == "sell"]
    assert len(sells) > 2_000
    assert result.final_positions


def test_a_closed_trade_qty_is_the_entry_quantity() -> None:
    """Two buys, then a full exit. qty is the sum of the entry fills, not 0."""
    document = _strategy(
        costs={"commission_bps": 0, "slippage_bps": 0},
        signals=[
            {"id": "go", "expr": _eq("close", 100)},
            {"id": "add", "expr": _eq("close", 150)},
            {"id": "stop", "expr": _eq("close", 140)},
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
            {"id": "add_rule", "when": "add", "do": "add_sol"},
            {"id": "exit_rule", "when": "stop", "do": "exit_sol"},
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
    result = run_backtest(
        document, {"sol": [_bar(0, 100), _bar(60, 150), _bar(120, 140)]}
    )
    trade = result.trades[0]
    buys = [fill.qty for fill in result.fills if fill.side == "buy"]
    assert trade.status == "closed"
    assert buys[0] == Decimal("50")
    assert buys[1] == floor_qty(Fraction(5000) / Fraction(150))
    assert trade.qty == buys[0] + buys[1]
    assert trade.qty != 0


def test_a_nan_gap_lints_the_seeded_run_before_the_gap() -> None:
    """The outage is only in the run that ends at the hole.

    The bars after the hole are too short for EMA(3), so the lint has to come
    from the run that the gap closed.
    """
    document = _strategy(
        derived_series=[
            {
                "id": "ema_3",
                "kind": "indicator",
                "indicator": "ema",
                "source": "close",
                "params": {"period": 3},
            }
        ],
        signals=[{"id": "go", "expr": _gt("close", 10_000)}],
    )
    bars = [
        _bar(0, 10, synthetic=True),
        _bar(60, 11, synthetic=True),
        _bar(120, 12, synthetic=True),
        _bar(240, 13),
    ]
    result = run_backtest(document, {"sol": bars}, long_no_trade_minutes=1)
    assert "ema_3 on sol is computed across 3 suspect_outage bars" in result.lints


def test_a_changed_open_basis_fails_the_run() -> None:
    """A sub-cent basis error is in the book, not in the checker."""
    import app.backtest.engine as engine

    real = engine._note_buy

    def planted(*args, **kwargs):
        real(*args, **kwargs)
        args[0][args[1]].basis += Fraction(1, 250)

    engine._note_buy = planted
    try:
        document = _strategy(
            costs={"commission_bps": 0, "slippage_bps": 0},
            signals=[{"id": "go", "expr": {"op": "lit", "value": True}}],
        )
        document["actions"][0]["risk"] = _risk(100, 99)
        try:
            run_backtest(document, {"sol": [_bar(0, 100), _bar(60, 100)]})
        except BacktestError as exc:
            assert "do not match the equity change" in str(exc)
        else:
            raise AssertionError("a 0.4 cent basis error was absorbed")
    finally:
        engine._note_buy = real


def test_two_open_trades_share_a_two_cent_remainder() -> None:
    def trade(asset: str, trade_id: int) -> Trade:
        return Trade(
            trade_id=trade_id,
            asset_id=asset,
            status="open",
            entry_time=0,
            exit_time=None,
            qty=Decimal("1"),
            entry_price=Decimal("1"),
            exit_price=None,
            entry_notional=Decimal("1.00"),
            exit_notional=Decimal("0.00"),
            commission=Decimal("0.00"),
            realized_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("1.00"),
            limit_below_low=False,
            overlaps_suspect_outage=False,
            synthetic_bars=0,
            missing_bars=0,
            long_no_trade_bars=0,
            long_no_trade_minutes=0,
            suspect_outage_bars=0,
            suspect_outage_minutes=0,
        )

    trades = (trade("sol", 1), trade("jup", 2))
    # Rounded sum is 2.00. The equity change is 2.02, so each trade takes one cent.
    spread = _spread_remainder(
        trades, (Fraction("1.006"), Fraction("1.004")), Decimal("0"), Decimal("2.02")
    )
    assert spread[0].unrealized_pnl == Decimal("1.01")
    assert spread[1].unrealized_pnl == Decimal("1.01")
    assert abs(spread[0].unrealized_pnl - Decimal("1.00")) <= Decimal("0.01")
    assert abs(spread[1].unrealized_pnl - Decimal("1.00")) <= Decimal("0.01")


def test_a_net_return_past_the_quantize_is_overflow() -> None:
    metrics = compute_metrics(
        initial=Decimal("1"),
        curve=(Decimal("1E+45"),),
        closed_pnls=(),
        open_trades=0,
        bar_seconds=86_400,
        take_profit_limit_below_low=0,
        quality=_quality(1),
    )
    assert metrics.net_return is None
    assert metrics.undefined["net_return"] == "overflow"
    assert metrics.undefined["annualized_return"] == "overflow"
    assert metrics.undefined["net_return"] != "annualization_overflow"


def test_a_zero_previous_equity_contributes_a_zero_return() -> None:
    metrics = compute_metrics(
        initial=Decimal("100"),
        curve=(Decimal("0"), Decimal("50")),
        closed_pnls=(),
        open_trades=0,
        bar_seconds=86_400,
        take_profit_limit_below_low=0,
        quality=_quality(2),
    )
    assert metrics.sharpe is not None
    assert metrics.sharpe.is_finite()
    assert "too_few_returns" not in metrics.undefined.values()
    assert metrics.sortino is not None
    assert metrics.net_return == Decimal("-0.5")


def test_a_half_tie_at_the_tenth_digit_rounds_half_even() -> None:
    """1.00000000005 ends on an even digit. Half-up would publish 1.0000000001."""
    metrics = compute_metrics(
        initial=Decimal("10000"),
        curve=(Decimal("20000.0000005"),),
        closed_pnls=(),
        open_trades=0,
        bar_seconds=60,
        take_profit_limit_below_low=0,
        quality=_quality(1),
    )
    assert metrics.net_return == Decimal("1")
    assert format(metrics.net_return, "f") == "1.0000000000"


def test_equal_nonterminating_returns_are_zero_variance() -> None:
    """1/11 and -1/3 do not terminate. The variance is still zero."""
    up = compute_metrics(
        initial=Decimal("1.21"),
        curve=(Decimal("1.32"), Decimal("1.44")),
        closed_pnls=(),
        open_trades=0,
        bar_seconds=86_400,
        take_profit_limit_below_low=0,
        quality=_quality(2),
    )
    assert up.undefined["sharpe"] == "zero_variance"
    down = compute_metrics(
        initial=Decimal("2.43"),
        curve=(Decimal("1.62"), Decimal("1.08"), Decimal("0.72"), Decimal("0.48")),
        closed_pnls=(),
        open_trades=0,
        bar_seconds=86_400,
        take_profit_limit_below_low=0,
        quality=_quality(4),
    )
    assert down.undefined["sharpe"] == "zero_variance"
    assert down.sortino is not None


def test_sale_cents_match_the_grid_quantity() -> None:
    """Half-even of the floored quantity times the sale price. No mismatches."""
    rng = random.Random(11)
    cases = [(43175, 78.834, 1.655514)]
    for _ in range(80):
        cases.append(
            (
                rng.randrange(1, 50_000),
                rng.randrange(1, 20_000) / 1000,
                rng.randrange(1, 20_000) / 1000,
            )
        )
    mismatches = 0
    for capital, price, sale in cases:
        if price == sale:
            continue
        document = _strategy(
            capital={"initial": capital, "currency": "USDC"},
            costs={"commission_bps": 0, "slippage_bps": 0},
            signals=[
                {"id": "go", "expr": _eq("close", price)},
                {"id": "stop", "expr": _eq("close", sale)},
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
        result = run_backtest(document, {"sol": [_bar(0, price), _bar(60, sale)]})
        if len(result.fills) != 2:
            mismatches += 1
            continue
        buy, sell = result.fills
        buy_priced = quantize_cents(Fraction(buy.qty) * Fraction(buy.price))
        sell_priced = quantize_cents(Fraction(sell.qty) * Fraction(sell.price))
        if buy.notional != buy_priced or sell.notional != sell_priced:
            mismatches += 1
    assert mismatches == 0


def test_calmar_overflows_while_the_annualized_return_stays_defined() -> None:
    """Doubling in three days annualizes inside 50 digits. The tiny dip does not.

    Calmar is that annualized return divided by a one-cent drawdown, which
    the 1e-10 quantize cannot hold. Annualized return itself stays a number.
    """
    metrics = compute_metrics(
        initial=Decimal("10000"),
        curve=(Decimal("10000"), Decimal("9999.99"), Decimal("20000")),
        closed_pnls=(),
        open_trades=1,
        bar_seconds=86_400,
        take_profit_limit_below_low=0,
        quality=_quality(3),
    )
    assert metrics.annualized_return is not None
    assert metrics.annualized_return.is_finite()
    assert metrics.max_drawdown == Decimal("0.000001")
    assert metrics.calmar is None
    assert metrics.undefined["calmar"] == "annualization_overflow"
    assert "annualized_return" not in metrics.undefined


def test_a_nan_gap_does_not_carry_a_later_outage_into_the_earlier_run() -> None:
    """Outage only after the hole. The second run is too short for a finite EMA.

    Removing the close-the-run branch makes the later outage part of the
    first run, and that run does have a finite EMA, so it would lint.
    An outage before the hole still lints.
    """
    document = _strategy(
        derived_series=[
            {
                "id": "ema_3",
                "kind": "indicator",
                "indicator": "ema",
                "source": "close",
                "params": {"period": 3},
            }
        ],
        signals=[{"id": "go", "expr": _gt("close", 10_000)}],
    )
    after = [
        _bar(0, 10),
        _bar(60, 11),
        _bar(120, 12),
        _bar(240, 13, synthetic=True),
        _bar(300, 14, synthetic=True),
    ]
    quiet = run_backtest(document, {"sol": after}, long_no_trade_minutes=1)
    assert quiet.metrics.suspect_outage is True
    assert not any("suspect_outage" in note for note in quiet.lints)
    before = [
        _bar(0, 10, synthetic=True),
        _bar(60, 11, synthetic=True),
        _bar(120, 12, synthetic=True),
        _bar(240, 13),
    ]
    tainted = run_backtest(document, {"sol": before}, long_no_trade_minutes=1)
    assert "ema_3 on sol is computed across 3 suspect_outage bars" in tainted.lints


def test_partial_sale_basis_is_the_1e_30_grid() -> None:
    """1e-20 and 1e-4 rounding of 10/9 are different numbers. The book uses 1e-30."""
    from app.backtest.engine import _sold_basis

    exact = Fraction(10, 3) * Fraction(1) / Fraction(3)

    def on(quantum: int) -> Fraction:
        return Fraction(round(exact * quantum), quantum)

    sold = _sold_basis(Fraction(10, 3), Decimal("1"), Decimal("3"))
    assert sold == on(10**30)
    assert sold != on(10**20)
    assert sold != on(10**4)

    import app.backtest.engine as engine

    seen: list[Fraction] = []
    real = engine._note_sell

    def record(*args, **kwargs):
        real(*args, **kwargs)
        asset_id = args[2]
        acc = args[0].get(asset_id)
        if acc is None:
            acc = args[1][-1]
        seen.append(acc.basis)

    engine._note_sell = record
    try:
        document, bars = _dca_trim(22, close_out=True)
        result = run_backtest(document, {"sol": bars})
    finally:
        engine._note_sell = real

    def replay(quantum: int) -> list[Fraction]:
        basis = Fraction(0)
        qty = Decimal(0)
        opened: list[Fraction] = []
        for fill in result.fills:
            if fill.side == "buy":
                basis += Fraction(fill.notional) + Fraction(fill.commission)
                qty += fill.qty
                continue
            if fill.qty == qty:
                basis = Fraction(0)
                qty = Decimal(0)
            else:
                share = basis * Fraction(fill.qty) / Fraction(qty)
                basis -= Fraction(round(share * quantum), quantum)
                qty -= fill.qty
            opened.append(basis)
        return opened

    assert seen == replay(10**30)
    assert seen != replay(10**20)
    assert seen != replay(10**4)
