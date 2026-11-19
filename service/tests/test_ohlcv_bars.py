"""Every OHLCV validation rule, one case at a time. Nothing is repaired."""

from __future__ import annotations

import math

import pytest

from app.ohlcv.bars import MAX_ABS_VALUE, Bar, validate_bar

_MINT = "Mint111111111111111111111111111111111111111"
_T = 1_700_000_040


def _bar(**overrides: object) -> Bar:
    fields: dict[str, object] = {
        "mint": _MINT,
        "timeframe": "1m",
        "open_time": _T,
        "open": 10.0,
        "high": 12.0,
        "low": 8.0,
        "close": 11.0,
        "volume": 3.0,
    }
    fields.update(overrides)
    return Bar(**fields)  # type: ignore[arg-type]


def test_a_well_formed_bar_has_no_reasons() -> None:
    assert validate_bar(_bar()) == []
    ceiling = _bar(
        open=MAX_ABS_VALUE,
        high=MAX_ABS_VALUE,
        low=MAX_ABS_VALUE,
        close=MAX_ABS_VALUE,
    )
    assert validate_bar(ceiling) == []


def test_non_positive_prices_are_rejected_and_zero_volume_is_kept() -> None:
    assert "open must be greater than 0" in validate_bar(_bar(open=0.0))
    zeros = _bar(open=0.0, high=0.0, low=0.0, close=0.0)
    assert "high must be greater than 0" in validate_bar(zeros)
    assert "low must be greater than 0" in validate_bar(_bar(low=0.0))
    assert "close must be greater than 0" in validate_bar(_bar(close=0.0))
    assert validate_bar(_bar(open=4.0, high=4.0, low=4.0, close=4.0, volume=0.0)) == []


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("open", math.nan, "open is not finite"),
        ("high", math.inf, "high is not finite"),
        ("low", math.nan, "low is not finite"),
        ("close", -math.inf, "close is not finite"),
        ("volume", math.nan, "volume is not finite"),
        ("open", -0.1, "open is negative"),
        ("high", -1.0, "high is negative"),
        ("low", -1.0, "low is negative"),
        ("close", -2.0, "close is negative"),
        ("volume", -0.01, "volume is negative"),
        ("open", MAX_ABS_VALUE + 1.0, "open is above 1e15"),
        ("high", 1e16, "high is above 1e15"),
        ("close", 1e16, "close is above 1e15"),
        ("volume", MAX_ABS_VALUE + 1.0, "volume is above 1e15"),
    ],
)
def test_each_finite_and_range_rule(field: str, value: float, reason: str) -> None:
    reasons = validate_bar(_bar(**{field: value}))
    assert reason in reasons


def test_low_above_the_ceiling_is_flagged() -> None:
    reasons = validate_bar(_bar(low=1e16, open=1e16, high=1e16, close=1e16))
    assert "low is above 1e15" in reasons


def test_high_must_cover_the_body() -> None:
    reasons = validate_bar(_bar(open=10.0, close=12.0, high=11.0, low=9.0))
    assert reasons == ["high is below max(open, close)"]


def test_low_must_not_cut_the_body() -> None:
    reasons = validate_bar(_bar(open=10.0, close=12.0, high=13.0, low=11.0))
    assert reasons == ["low is above min(open, close)"]


def test_high_below_low_reports_the_broken_order() -> None:
    reasons = validate_bar(_bar(open=3.0, close=3.0, high=1.0, low=4.0))
    assert "high is below max(open, close)" in reasons
    assert "low is above min(open, close)" in reasons


def test_alignment_timeframe_and_mint() -> None:
    assert validate_bar(_bar(open_time=_T + 30)) == ["open_time is not aligned to 1m"]
    assert validate_bar(_bar(timeframe="2m")) == ["timeframe is not supported"]
    assert validate_bar(_bar(mint="")) == ["mint is empty"]


def test_a_synthetic_bar_must_be_flat_with_zero_volume() -> None:
    flat = _bar(open=4.0, high=4.0, low=4.0, close=4.0, volume=0.0, synthetic=True)
    assert validate_bar(flat) == []
    traded = _bar(synthetic=True)
    assert "synthetic bar must be flat" in validate_bar(traded)
    assert "synthetic bar must have volume 0" in validate_bar(traded)


def test_a_flat_bar_is_valid_market_data() -> None:
    assert validate_bar(_bar(open=4.0, high=4.0, low=4.0, close=4.0, volume=0.0)) == []
