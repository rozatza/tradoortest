"""Metrics, the trade log, and the data-quality counts.

The hand arithmetic is in the test docstrings. The bars are synthetic.
Nothing here calls the network.
"""

from __future__ import annotations

import csv
import io
import json
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

from app.backtest import TRADE_COLUMNS, run_backtest, trade_log_csv, trade_log_json
from app.backtest.log import SCHEMA
from app.backtest.money import quantize_cents
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
        assert getattr(result.metrics, name).is_finite()
    for trade in result.trades:
        assert trade.realized_pnl.is_finite()
        assert trade.unrealized_pnl.is_finite()


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
    """43175 / 78.834 * 1.655514 = 906.675 exactly.

    Half-even takes the odd cent up to 906.68. The 50-digit quantity is
    906.67499... and would keep 906.67.
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
    assert result.fills[1].notional == Decimal("906.68")
    assert result.final_cash == Decimal("906.68")
    assert result.trades[0].exit_notional == Decimal("906.68")
    assert result.trades[0].realized_pnl == Decimal("-42268.32")
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
    assert result.metrics.payoff_ratio == Decimal("0")
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
    assert rows == payload["trades"]
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
    assert result.metrics.annualized_return == Decimal("0")
    assert result.metrics.max_drawdown == Decimal("0")
    assert result.metrics.sharpe == Decimal("0")
    assert result.metrics.sortino == Decimal("0")
    assert result.metrics.calmar == Decimal("0")
    assert result.metrics.win_rate == Decimal("0")
    assert result.metrics.payoff_ratio == Decimal("0")
    assert result.metrics.win_streak == 0
    assert result.metrics.loss_streak == 0
    assert result.metrics.closed_trades == 0
    assert result.metrics.bar_count == 3


def test_no_losses_report_zero_payoff_sortino_and_calmar() -> None:
    """One winning sale. Equity never drops, and no bar return is negative.

    Payoff has no loss to divide by. Sortino's downside deviation is 0.
    Calmar's drawdown is 0. Each of those is 0, not an infinity. Sharpe is
    defined: the two returns are 0 and 0.1.
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
    assert result.metrics.payoff_ratio == Decimal("0")
    assert result.metrics.sortino == Decimal("0")
    assert result.metrics.calmar == Decimal("0")
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
    assert result.metrics.win_rate == Decimal("0")
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
    assert row["exit_time"] == ""
    assert row["exit_price"] == ""
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
    assert result.metrics.payoff_ratio == Decimal("0")
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
    assert result.metrics.net_return == Decimal("0")
    assert result.metrics.sharpe == Decimal("0")


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
