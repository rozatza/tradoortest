"""Higher timeframes are a pure function of complete 1m buckets."""

from __future__ import annotations

import pytest

from app.ohlcv.bars import Bar
from app.ohlcv.resample import resample

_MINT = "Mint111111111111111111111111111111111111111"
_START = 1_700_000_100  # aligned to 5m and to 1m


def _minute(
    offset: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> Bar:
    return Bar(_MINT, "1m", _START + offset, open_, high, low, close, volume)


def _five() -> list[Bar]:
    return [
        _minute(0, open_=1.0, high=1.0, low=1.0, close=1.0, volume=0.5),
        _minute(60, open_=2.0, high=3.0, low=1.0, close=2.0, volume=0.5),
        _minute(120, open_=3.0, high=3.0, low=2.0, close=3.0, volume=1.0),
        _minute(180, open_=4.0, high=4.0, low=4.0, close=4.0, volume=1.0),
        _minute(240, open_=5.0, high=9.0, low=4.0, close=8.0, volume=2.0),
    ]


def test_five_complete_minutes_become_one_bar() -> None:
    bars = resample(_five(), "5m")
    assert len(bars) == 1
    bar = bars[0]
    assert bar.timeframe == "5m"
    assert bar.open_time == _START
    assert bar.open == 1.0
    assert bar.high == 9.0
    assert bar.low == 1.0
    assert bar.close == 8.0
    assert bar.volume == 5.0


def test_input_order_does_not_change_the_bar() -> None:
    forward = resample(_five(), "5m")
    backward = resample(list(reversed(_five())), "5m")
    assert forward == backward


def test_a_missing_minute_leaves_the_bucket_empty() -> None:
    short = [bar for bar in _five() if bar.open_time != _START + 120]
    assert resample(short, "5m") == []


def test_a_duplicate_minute_suppresses_the_bucket() -> None:
    duplicated = [*_five(), _five()[2]]
    assert resample(duplicated, "5m") == []


def test_one_minute_is_sorted_and_not_filled() -> None:
    bars = list(reversed(_five()[:2]))
    assert [bar.open_time for bar in resample(bars, "1m")] == [_START, _START + 60]


def test_resample_refuses_mixed_inputs() -> None:
    other = Bar("OtherMint", "1m", _START, 1.0, 1.0, 1.0, 1.0, 1.0)
    with pytest.raises(ValueError, match="one mint"):
        resample([*_five(), other], "5m")
    already = Bar(_MINT, "5m", _START, 1.0, 1.0, 1.0, 1.0, 1.0)
    with pytest.raises(ValueError, match="1m bars"):
        resample([already], "1h")
