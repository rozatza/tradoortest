"""Canonical IR numbers arrive as plain floats and ints.

Numpy scalars and dicts are rejected on the indicator boundary. The explicit
converters are the only path that accepts a numpy-shaped value.
"""

from __future__ import annotations

import pytest

from app.indicators import (
    IndicatorError,
    as_float_series,
    as_period,
    floats_from_numpy,
    period_from_numpy,
    sma,
)


class NumpyFloat64(float):
    """Stand-in for ``numpy.float64``. The real type's module is ``numpy``."""

    __module__ = "numpy"


class NumpyInt64(int):
    """Stand-in for ``numpy.int64``. ``isinstance(np.int64(1), int)`` is false.

    Subclassing ``int`` makes the rejection stronger: the boundary must key
    off the numpy module, not off ``isinstance(..., int)``.
    """

    __module__ = "numpy"


class NumpyArray:
    """Stand-in for a 1-d ``numpy.ndarray``."""

    __module__ = "numpy"

    def __init__(self, values: list[object]) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)


def test_plain_python_floats_and_ints_pass() -> None:
    assert as_float_series([1, 2.5], "close") == [1.0, 2.5]
    assert type(as_float_series([1], "close")[0]) is float
    assert as_period(14, "period") == 14
    assert type(as_period(14, "period")) is int
    assert sma([1.0, 2.0, 3.0], 2)[1:] == [1.5, 2.5]


def test_sma_accepts_only_plain_values_after_explicit_conversion() -> None:
    raw = NumpyArray([NumpyFloat64(1.0), NumpyFloat64(3.0), NumpyFloat64(5.0)])
    period = NumpyInt64(2)
    with pytest.raises(IndicatorError, match="numpy"):
        sma(raw, 2)  # type: ignore[arg-type]
    with pytest.raises(IndicatorError, match="numpy"):
        sma([1.0, 3.0, 5.0], period)  # type: ignore[arg-type]
    converted = floats_from_numpy(raw, "close")
    assert converted == [1.0, 3.0, 5.0]
    assert all(type(item) is float for item in converted)
    plain_period = period_from_numpy(period, "period")
    assert plain_period == 2
    assert type(plain_period) is int
    assert sma(converted, plain_period)[1:] == [2.0, 4.0]


def test_dicts_are_rejected_and_not_iterated_as_keys() -> None:
    with pytest.raises(IndicatorError, match="dict"):
        as_float_series({"close": [1.0, 2.0]}, "close")
    with pytest.raises(IndicatorError, match="dict"):
        sma([{"open": 1.0}], 1)  # type: ignore[list-item]
    with pytest.raises(IndicatorError, match="dict"):
        as_period({"period": 14}, "period")
    with pytest.raises(IndicatorError, match="dict"):
        floats_from_numpy({"close": [1.0]}, "close")
    with pytest.raises(IndicatorError, match="dict"):
        period_from_numpy({"period": 14}, "period")


def test_numpy_float_period_is_not_an_integer_period() -> None:
    with pytest.raises(IndicatorError, match="numpy integer"):
        period_from_numpy(NumpyFloat64(14.0), "period")
