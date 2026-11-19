"""Golden values and warmup edges for the indicator library."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.indicators import (
    IndicatorError,
    atr,
    atr_warmup,
    bollinger,
    bollinger_warmup,
    ema,
    ema_warmup,
    macd,
    macd_warmup,
    rsi,
    rsi_warmup,
    sma,
    sma_warmup,
    stochastic,
    stochastic_warmup,
)

FIXTURE = Path(__file__).parent / "fixtures" / "indicators" / "golden.json"
REL_TOL = 1e-9
ABS_TOL = 1e-9


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _floats(values: list[float | None]) -> list[float]:
    return [math.nan if value is None else float(value) for value in values]


def _close(actual: float, expected: float) -> bool:
    if math.isnan(expected):
        return math.isnan(actual)
    if math.isnan(actual) or math.isinf(actual) or math.isinf(expected):
        return False
    return math.isclose(actual, expected, rel_tol=REL_TOL, abs_tol=ABS_TOL)


def _assert_series(actual: list[float], expected: list[float], label: str) -> None:
    assert len(actual) == len(expected), label
    for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
        assert _close(got, want), f"{label}[{index}]: {got} != {want}"


def _assert_prefix(values: list[float], warmup: int) -> None:
    assert all(math.isnan(value) for value in values[:warmup])
    assert warmup == len(values) or math.isfinite(values[warmup])
    assert not any(math.isinf(value) for value in values)


def test_golden_fixture_names_the_reference() -> None:
    data = _load()
    assert data["reference"] == "TA-Lib"
    assert data["reference_version"] == "0.6.8"
    assert data["reference_c_library"].startswith("0.6.4")
    assert data["seed"] == 68
    assert data["tolerance"] == {"rel": REL_TOL, "abs": ABS_TOL}
    assert {case["name"] for case in data["indicators"]} == {
        "sma",
        "ema",
        "rsi",
        "atr",
        "macd",
        "bollinger",
        "stochastic",
    }


def test_golden_values_and_warmup_counts() -> None:
    data = _load()
    ohlcv = {name: _floats(values) for name, values in data["ohlcv"].items()}
    close = ohlcv["close"]
    for case in data["indicators"]:
        outputs = _compute(case["name"], case["params"], ohlcv)
        for name, raw in case["outputs"].items():
            expected = _floats(raw)
            label = f"{case['name']}.{name}"
            _assert_series(outputs[name], expected, label)
            _assert_prefix(outputs[name], case["warmup"][name])
            assert len(outputs[name]) == len(close)


def _compute(name: str, params: dict, ohlcv: dict[str, list[float]]) -> dict[str, list[float]]:
    close = ohlcv["close"]
    high = ohlcv["high"]
    low = ohlcv["low"]
    if name == "sma":
        return {"values": sma(close, params["period"])}
    if name == "ema":
        return {"values": ema(close, params["period"])}
    if name == "rsi":
        return {"values": rsi(close, params["period"])}
    if name == "atr":
        return {"values": atr(high, low, close, params["period"])}
    if name == "macd":
        result = macd(close, params["fast"], params["slow"], params["signal"])
        return {"macd": result.macd, "signal": result.signal, "histogram": result.histogram}
    if name == "bollinger":
        result = bollinger(close, params["period"], params["stddev"])
        return {"upper": result.upper, "middle": result.middle, "lower": result.lower}
    if name == "stochastic":
        result = stochastic(high, low, close, params["k_period"], params["d_period"])
        return {"k": result.k, "d": result.d}
    raise AssertionError(name)


def test_documented_warmup_lengths() -> None:
    assert sma_warmup(14) == 13
    assert ema_warmup(12) == 11
    assert rsi_warmup(14) == 14
    assert atr_warmup(14) == 14
    assert macd_warmup(12, 26, 9) == (25, 33, 33)
    assert bollinger_warmup(20) == 19
    assert stochastic_warmup(14, 3) == (13, 15)
    assert sma_warmup(1) == 0
    assert ema_warmup(1) == 0
    assert rsi_warmup(1) == 1


def test_period_one_matches_the_definition() -> None:
    values = [1.0, 2.0, 4.0]
    assert sma(values, 1) == values
    assert ema(values, 1) == values
    # One change of +1, then +2. Both are gains, so RSI is 100 after the first bar.
    rsi_values = rsi(values, 1)
    assert math.isnan(rsi_values[0])
    assert rsi_values[1:] == [100.0, 100.0]
    bands = bollinger(values, 1, 2)
    assert bands.middle == values
    assert bands.upper == values
    assert bands.lower == values
    high = [2.0, 4.0, 4.0]
    low = [0.0, 2.0, 4.0]
    close = [1.0, 3.0, 4.0]
    stoch = stochastic(high, low, close, 1, 1)
    assert stoch.k == [50.0, 50.0, 0.0]
    assert stoch.d == stoch.k
    ranges = atr(high, low, close, 1)
    assert math.isnan(ranges[0])
    assert ranges[1] == max(2.0, abs(4.0 - 1.0), abs(2.0 - 1.0))
    assert ranges[2] == max(0.0, abs(4.0 - 3.0), abs(4.0 - 3.0))


def test_period_longer_than_the_input_is_all_nan() -> None:
    values = [1.0, 2.0, 3.0]
    high = [2.0, 3.0, 4.0]
    low = [0.5, 1.0, 1.5]
    assert all(math.isnan(value) for value in sma(values, 4))
    assert all(math.isnan(value) for value in ema(values, 4))
    assert all(math.isnan(value) for value in rsi(values, 4))
    assert all(math.isnan(value) for value in atr(high, low, values, 4))
    result = macd(values, 3, 4, 2)
    assert all(math.isnan(value) for value in result.macd)
    assert all(math.isnan(value) for value in result.signal)
    assert all(math.isnan(value) for value in result.histogram)
    bands = bollinger(values, 4, 2)
    assert all(math.isnan(value) for value in (*bands.upper, *bands.middle, *bands.lower))
    stoch = stochastic(high, low, values, 4, 2)
    assert all(math.isnan(value) for value in (*stoch.k, *stoch.d))
    assert sma([], 14) == []
    assert rsi([], 14) == []


def test_macd_does_not_swap_fast_and_slow() -> None:
    values = [float(index) for index in range(1, 12)]
    forward = macd(values, 2, 4, 2)
    backward = macd(values, 4, 2, 2)
    for left, right in zip(forward.macd, backward.macd, strict=True):
        if math.isfinite(left):
            assert math.isclose(left, -right)


def test_infinity_in_the_input_is_a_gap() -> None:
    values = [1.0, 2.0, 3.0, math.inf, 5.0, 6.0, 7.0]
    result = sma(values, 3)
    assert math.isnan(result[3]) and math.isnan(result[4]) and math.isnan(result[5])
    assert result[6] == 6.0
    assert not any(math.isinf(value) for value in result)


def test_bollinger_stddev_of_ten_uses_population_sd() -> None:
    # Mean 2, population sd 1. Width 10 is the schema maximum.
    bands = bollinger([1.0, 3.0], 2, 10)
    assert math.isnan(bands.middle[0])
    assert bands.middle[1] == 2.0
    assert bands.upper[1] == 12.0
    assert bands.lower[1] == -8.0


def test_constant_rsi_and_stochastic_stay_finite() -> None:
    flat = [7.0] * 12
    rsi_values = rsi(flat, 5)
    _assert_prefix(rsi_values, 5)
    assert rsi_values[5:] == [0.0] * 7
    stoch = stochastic(flat, flat, flat, 4, 3)
    _assert_prefix(stoch.k, 3)
    _assert_prefix(stoch.d, 5)
    assert stoch.k[3:] == [0.0] * 9
    assert stoch.d[5:] == [0.0] * 7
    bands = bollinger(flat, 4, 2)
    assert bands.middle[3:] == [7.0] * 9
    assert bands.upper[3:] == [7.0] * 9
    assert bands.lower[3:] == [7.0] * 9


def test_rsi_wilder_step_on_a_short_series() -> None:
    # Changes +1, -1, +2. First RSI uses the first two changes.
    values = rsi([10.0, 11.0, 10.0, 12.0], 2)
    assert math.isnan(values[0]) and math.isnan(values[1])
    assert values[2] == 50.0
    assert math.isclose(values[3], 100.0 * 1.25 / 1.5)


def test_atr_wilder_step_on_a_short_series() -> None:
    high = [10.0, 12.0, 11.0, 13.0]
    low = [8.0, 9.0, 9.0, 10.0]
    close = [9.0, 11.0, 10.0, 12.0]
    values = atr(high, low, close, 2)
    assert math.isnan(values[0]) and math.isnan(values[1])
    # TR = 3, 2, 3. Seed is the mean of the first two. Next bar is Wilder.
    assert values[2] == 2.5
    assert values[3] == (2.5 + 3.0) / 2


def test_sma_nan_gap_resumes_without_filling() -> None:
    values = [1.0, 2.0, 3.0, math.nan, 5.0, 6.0, 7.0, 8.0]
    result = sma(values, 3)
    assert result[2] == 2.0
    assert all(math.isnan(value) for value in result[3:6])
    assert result[6] == 6.0
    assert result[7] == 7.0


def test_ema_nan_gap_reseeds_instead_of_carrying_state() -> None:
    values = [1.0, 2.0, 3.0, 4.0, math.nan, 1.0, 2.0, 3.0, 10.0]
    result = ema(values, 3)
    assert result[2] == 2.0
    assert result[3] == 3.0
    assert all(math.isnan(value) for value in result[4:7])
    assert result[7] == 2.0
    assert result[8] == 6.0


def test_rsi_nan_gap_does_not_forward_fill() -> None:
    # The run before the gap is all gains (RSI 100). The run after is all losses.
    values = [1.0, 2.0, 3.0, 4.0, 5.0, math.nan, 4.0, 3.0, 2.0, 1.0]
    result = rsi(values, 3)
    assert result[3] == 100.0
    assert result[4] == 100.0
    assert all(math.isnan(value) for value in result[5:9])
    assert result[9] == 0.0


def test_bollinger_and_stochastic_nan_windows() -> None:
    values = [1.0, 2.0, 3.0, math.nan, 3.0, 3.0, 3.0]
    bands = bollinger(values, 3, 2)
    assert math.isfinite(bands.middle[2])
    assert all(math.isnan(value) for value in bands.middle[3:6])
    assert bands.middle[6] == 3.0
    assert bands.upper[6] == 3.0
    high = [2.0, 3.0, 4.0, math.nan, 4.0, 4.0, 4.0]
    low = [0.0, 1.0, 2.0, 1.0, 2.0, 2.0, 2.0]
    close = [1.0, 2.0, 3.0, 2.0, 3.0, 3.0, 3.0]
    stoch = stochastic(high, low, close, 3, 1)
    assert math.isfinite(stoch.k[2])
    assert math.isnan(stoch.k[3]) and math.isnan(stoch.k[4]) and math.isnan(stoch.k[5])
    assert math.isfinite(stoch.k[6])


def test_macd_gap_drops_the_line_until_both_emas_reseed() -> None:
    values = [float(index) for index in range(1, 15)]
    values[6] = math.nan
    result = macd(values, 2, 3, 2)
    assert math.isnan(result.macd[6])
    assert math.isnan(result.signal[6])
    assert math.isnan(result.histogram[6])
    # Slow EMA needs 3 finite prices after the gap, so the next two bars stay empty.
    assert math.isnan(result.macd[7]) and math.isnan(result.macd[8])
    assert math.isfinite(result.macd[9])
    assert not any(math.isinf(value) for value in (*result.macd, *result.signal, *result.histogram))


@pytest.mark.parametrize(
    "call",
    [
        lambda: sma([1.0, 2.0], 0),
        lambda: sma([1.0, 2.0], -1),
        lambda: sma([1.0, 2.0], 501),
        lambda: ema([1.0], True),  # type: ignore[arg-type]
        lambda: rsi([1.0, 2.0], 1.5),  # type: ignore[arg-type]
        lambda: macd([1.0, 2.0], 2, 3, 0),
        lambda: bollinger([1.0, 2.0, 3.0], 2, 0),
        lambda: bollinger([1.0, 2.0, 3.0], 2, -1),
        lambda: bollinger([1.0, 2.0, 3.0], 2, 10.1),
        lambda: bollinger([1.0, 2.0], 2, math.nan),
        lambda: sma("123", 1),  # type: ignore[arg-type]
        lambda: sma([1.0, None], 1),  # type: ignore[list-item]
        lambda: atr([1.0, 2.0], [1.0], [1.0, 2.0], 1),
        lambda: stochastic([1.0], [1.0, 2.0], [1.0], 1, 1),
    ],
)
def test_invalid_arguments_raise(call) -> None:
    with pytest.raises(IndicatorError):
        call()


def test_length_mismatch_names_the_series() -> None:
    with pytest.raises(IndicatorError, match="low length 1 does not match high length 2"):
        atr([1.0, 2.0], [1.0], [1.0, 2.0], 2)


def test_indicator_package_does_not_import_a_reference_library() -> None:
    root = Path(__file__).resolve().parents[1] / "app" / "indicators"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    lowered = text.lower()
    assert "talib" not in lowered
    assert "pandas" not in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered
