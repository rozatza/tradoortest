"""Write golden indicator fixtures from TA-Lib.

Reference library: the PyPI package ``TA-Lib`` (import name ``talib``), which
bundles the TA-Lib C library. This script records both versions into
``golden.json``. It is not part of ``make test`` or CI. CI compares the
committed fixture and does not build TA-Lib.

Regenerate from a virtualenv that has ``TA-Lib`` and ``numpy`` installed
(the versions named in the fixture's metadata):

    python service/tests/fixtures/indicators/generate_golden.py

The series is a seeded synthetic random walk. Nothing is downloaded.

MACD in this fixture is ``TA-Lib EMA(fast) - TA-Lib EMA(slow)``, then an EMA
of that line. TA-Lib's own ``MACD()`` re-seeds the faster EMA at the slower
EMA's first bar, so it is not that difference. Stochastic ``%K`` is built
from ``TA-Lib MIN`` and ``MAX`` (no extra smoothing). ``%D`` is an SMA of
``%K``. The script checks the overlapping bars against ``TA-Lib STOCHF``.
Bollinger uses ``TA-Lib BBANDS`` (population standard deviation, ddof 0).
RSI and ATR are ``TA-Lib RSI`` and ``TA-Lib ATR`` (Wilder). EMA is seeded
with an SMA, which is what ``TA-Lib EMA`` does.

``null`` in the JSON is NaN.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import talib

SEED = 68
BARS = 240
OUT = Path(__file__).with_name("golden.json")
ABS_TOL = 1e-9
REL_TOL = 1e-9


def main() -> None:
    open_, high, low, close, volume = _synthetic(BARS, SEED)
    document = {
        "reference": "TA-Lib",
        "reference_version": talib.__version__,
        "reference_c_library": _c_version(),
        "numpy_version": np.__version__,
        "seed": SEED,
        "tolerance": {"rel": REL_TOL, "abs": ABS_TOL},
        "notes": {
            "ema": "TA-Lib EMA. Seed is an SMA, then alpha = 2/(period+1).",
            "rsi": "TA-Lib RSI. Wilder smoothing. Both averages at 0 yield 0.",
            "atr": "TA-Lib ATR. Wilder smoothing of true range. Warmup is the period.",
            "bollinger": "TA-Lib BBANDS with matype SMA. Population stddev, ddof 0.",
            "macd": (
                "TA-Lib EMA(fast) minus TA-Lib EMA(slow), then TA-Lib EMA of that "
                "line. TA-Lib MACD() re-seeds the faster EMA and is not this series."
            ),
            "stochastic": (
                "Fast %K from TA-Lib MIN and MAX, no extra smoothing. %D is an SMA. "
                "Overlapping bars were checked against TA-Lib STOCHF. A zero range is 0."
            ),
        },
        "ohlcv": {
            "open": _dump(open_),
            "high": _dump(high),
            "low": _dump(low),
            "close": _dump(close),
            "volume": _dump(volume),
        },
        "indicators": [
            _case("sma", {"period": 14}, {"values": talib.SMA(close, 14)}, {"values": 13}),
            _case("ema", {"period": 12}, {"values": talib.EMA(close, 12)}, {"values": 11}),
            _case("rsi", {"period": 14}, {"values": talib.RSI(close, 14)}, {"values": 14}),
            _case(
                "atr",
                {"period": 14},
                {"values": talib.ATR(high, low, close, 14)},
                {"values": 14},
            ),
            _macd_case(close),
            _bollinger_case(close),
            _stochastic_case(high, low, close),
        ],
    }
    OUT.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


def _macd_case(close: np.ndarray) -> dict:
    fast, slow, signal = 12, 26, 9
    line = talib.EMA(close, fast) - talib.EMA(close, slow)
    start = _first_finite(line)
    signal_line = np.full(len(close), np.nan)
    signal_line[start:] = talib.EMA(line[start:], signal)
    histogram = line - signal_line
    line_warmup = max(fast, slow) - 1
    signal_warmup = line_warmup + signal - 1
    if start != line_warmup:
        raise SystemExit(f"MACD line warmup {start} != {line_warmup}")
    if _first_finite(signal_line) != signal_warmup:
        raise SystemExit("MACD signal warmup does not match the EMA-of-line definition")
    return _case(
        "macd",
        {"fast": fast, "slow": slow, "signal": signal},
        {"macd": line, "signal": signal_line, "histogram": histogram},
        {"macd": line_warmup, "signal": signal_warmup, "histogram": signal_warmup},
    )


def _bollinger_case(close: np.ndarray) -> dict:
    upper, middle, lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
    return _case(
        "bollinger",
        {"period": 20, "stddev": 2},
        {"upper": upper, "middle": middle, "lower": lower},
        {"upper": 19, "middle": 19, "lower": 19},
    )


def _stochastic_case(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> dict:
    k_period, d_period = 14, 3
    lowest = talib.MIN(low, k_period)
    highest = talib.MAX(high, k_period)
    k = np.full(len(close), np.nan)
    for index in range(len(close)):
        if not (
            math.isfinite(lowest[index])
            and math.isfinite(highest[index])
            and math.isfinite(close[index])
        ):
            continue
        span = float(highest[index] - lowest[index])
        if span == 0.0:
            k[index] = 0.0
        else:
            k[index] = 100.0 * (float(close[index]) - float(lowest[index])) / span
    d = _sma(k, d_period)
    reference_k, reference_d = talib.STOCHF(high, low, close, k_period, d_period, fastd_matype=0)
    _assert_overlap(k, reference_k, "stochastic %K vs STOCHF")
    _assert_overlap(d, reference_d, "stochastic %D vs STOCHF")
    finite_close = np.asarray(close, dtype=float)
    _assert_overlap(_sma(finite_close, 14), talib.SMA(finite_close, 14), "reference SMA")
    return _case(
        "stochastic",
        {"k_period": k_period, "d_period": d_period},
        {"k": k, "d": d},
        {"k": k_period - 1, "d": k_period + d_period - 2},
    )


def _sma(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        if np.all(np.isfinite(window)):
            out[index] = float(np.sum(window) / period)
    return out


def _case(name: str, params: dict, outputs: dict[str, np.ndarray], warmup: dict[str, int]) -> dict:
    dumped = {key: _dump(value) for key, value in outputs.items()}
    for key, count in warmup.items():
        series = dumped[key]
        if any(item is not None for item in series[:count]):
            raise SystemExit(f"{name}.{key} warmup is not NaN")
        if count < len(series) and series[count] is None:
            raise SystemExit(f"{name}.{key} has no value at the end of warmup")
    return {"name": name, "params": params, "warmup": warmup, "outputs": dumped}


def _synthetic(
    n: int, seed: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, n)))
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    wick = rng.uniform(0.001, 0.01, n) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick * rng.uniform(0.4, 1.0, n)
    volume = rng.uniform(1_000.0, 5_000.0, n)
    return open_, high, low, close, volume


def _dump(values: np.ndarray) -> list[float | None]:
    dumped: list[float | None] = []
    for value in np.asarray(values, dtype=float):
        if math.isfinite(float(value)):
            dumped.append(float(value))
        else:
            dumped.append(None)
    return dumped


def _first_finite(values: np.ndarray) -> int:
    finite = np.where(np.isfinite(values))[0]
    if len(finite) == 0:
        raise SystemExit("series has no finite value")
    return int(finite[0])


def _assert_overlap(actual: np.ndarray, reference: np.ndarray, label: str) -> None:
    mask = np.isfinite(reference)
    if not np.any(mask):
        raise SystemExit(f"{label} reference is empty")
    delta = np.abs(actual[mask] - reference[mask])
    scale = np.maximum(1.0, np.abs(reference[mask]))
    if np.any(~np.isfinite(actual[mask])) or np.max(delta / scale) > 1e-9:
        raise SystemExit(f"{label} disagrees with TA-Lib (max abs {np.nanmax(delta)})")


def _c_version() -> str:
    raw = talib.__ta_version__
    if isinstance(raw, bytes):
        raw = raw.decode()
    return str(raw).split()[0]


if __name__ == "__main__":
    main()
