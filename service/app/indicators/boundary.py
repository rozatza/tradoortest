"""Boundary between canonical IR numbers and the indicator functions.

Indicators take a list of builtin ``float`` values and a builtin ``int``
period. They do not take numpy scalars, numpy arrays, or dicts. A caller
that still holds numpy values converts them here, on purpose, and then
passes the plain result in.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from app.indicators.errors import IndicatorError

_MAX_PERIOD = 500


def as_float_series(values: object, name: str = "values") -> list[float]:
    """Copy a canonical numeric sequence into plain Python floats.

    Rejects a dict, a numpy container, and any numpy scalar element. Convert
    those with ``floats_from_numpy`` first.
    """
    if isinstance(values, Mapping):
        raise IndicatorError(f"{name} must be a sequence of plain Python floats, not a dict")
    if _is_numpy(values):
        raise IndicatorError(
            f"{name} is a numpy value; convert it with floats_from_numpy before calling"
        )
    if isinstance(values, (str, bytes)) or values is None:
        raise IndicatorError(f"{name} must be a sequence of plain Python floats")
    try:
        iterator = iter(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise IndicatorError(f"{name} must be a sequence of plain Python floats") from exc
    data: list[float] = []
    for item in iterator:
        if isinstance(item, Mapping):
            raise IndicatorError(f"{name} must be a sequence of plain Python floats, not a dict")
        if _is_numpy(item):
            raise IndicatorError(
                f"{name} contains a numpy value; convert it with floats_from_numpy before calling"
            )
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise IndicatorError(f"{name} must be a sequence of plain Python floats")
        data.append(float(item))
    return data


def as_period(value: object, name: str = "period") -> int:
    """Return a builtin int period from 1 to 500.

    A numpy integer is rejected. Convert it with ``period_from_numpy`` first.
    ``bool`` is rejected even though it is a subclass of ``int``.
    """
    if isinstance(value, Mapping):
        raise IndicatorError(f"{name} must be a plain Python int, not a dict")
    if _is_numpy(value):
        raise IndicatorError(
            f"{name} is a numpy value; convert it with period_from_numpy before calling"
        )
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PERIOD:
        raise IndicatorError(f"{name} must be an integer from 1 to 500")
    return value


def floats_from_numpy(values: object, name: str = "values") -> list[float]:
    """Explicitly convert a numpy array or a sequence of numpy scalars.

    The result is a list of builtin ``float``. A dict is rejected. Plain
    Python ints and floats are accepted so a mixed sequence still becomes
    the canonical list. Non-finite values are preserved (they are gaps).
    """
    if isinstance(values, Mapping):
        raise IndicatorError(f"{name} must be a numpy sequence, not a dict")
    if isinstance(values, (str, bytes)) or values is None:
        raise IndicatorError(f"{name} must be a numpy sequence of numbers")
    try:
        iterator = iter(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise IndicatorError(f"{name} must be a numpy sequence of numbers") from exc
    data: list[float] = []
    for item in iterator:
        if isinstance(item, Mapping):
            raise IndicatorError(f"{name} must be a numpy sequence of numbers, not a dict")
        if isinstance(item, bool) or not _is_real_number(item):
            raise IndicatorError(f"{name} must be a numpy sequence of numbers")
        data.append(float(item))
    return data


def period_from_numpy(value: object, name: str = "period") -> int:
    """Explicitly convert one numpy integer scalar to a builtin ``int``."""
    if isinstance(value, Mapping):
        raise IndicatorError(f"{name} must be a numpy integer, not a dict")
    if isinstance(value, bool) or not _is_numpy(value) or not _is_integral(value):
        raise IndicatorError(f"{name} must be a numpy integer")
    period = int(value)  # type: ignore[arg-type]
    if not 1 <= period <= _MAX_PERIOD:
        raise IndicatorError(f"{name} must be an integer from 1 to 500")
    return period


def _is_numpy(value: object) -> bool:
    module = type(value).__module__
    return module == "numpy" or module.startswith("numpy.")


def _is_real_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if _is_numpy(value):
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return isinstance(number, float)
    return False


def _is_integral(value: object) -> bool:
    """True for a numpy integer scalar, false for a numpy float."""
    if isinstance(value, bool) or isinstance(value, float):
        return False
    try:
        number = int(value)  # type: ignore[arg-type]
        as_float = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return as_float == number and math.isfinite(as_float)
