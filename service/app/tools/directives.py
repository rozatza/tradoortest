"""Chart and indicator directives for the later Phase 2 UI.

Both tools validate against the Phase 1 catalog and return a document.
They do not read a market and they do not write a store.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.auth import CurrentUser
from app.indicators.boundary import as_period
from app.indicators.errors import IndicatorError
from app.indicators.library import (
    _band_width as band_width,
)
from app.indicators.library import (
    atr_warmup,
    bollinger_warmup,
    ema_warmup,
    macd_warmup,
    rsi_warmup,
    sma_warmup,
    stochastic_warmup,
)
from app.ir.constants import BUILTIN_SERIES, INDICATOR_PARAMS
from app.ohlcv.errors import InvalidMint
from app.ohlcv.mint import require_mint
from app.ohlcv.registry import BY_MINT, BY_SYMBOL
from app.tools.errors import ToolError, safe_text
from app.tools.runtime import ToolRuntime

_OUTPUTS = {
    "sma": ("sma",),
    "ema": ("ema",),
    "atr": ("atr",),
    "rsi": ("rsi",),
    "macd": ("macd", "signal", "histogram"),
    "bollinger": ("lower", "middle", "upper"),
    "stochastic": ("k", "d"),
}
_PANES = {
    "sma": "overlay",
    "ema": "overlay",
    "bollinger": "overlay",
    "rsi": "separate",
    "macd": "separate",
    "atr": "separate",
    "stochastic": "separate",
}
_DIRECTIVE_VERSION = "1.0"


def add_chart(
    arguments: Mapping[str, Any],
    _user: CurrentUser,
    _runtime: ToolRuntime,
) -> dict[str, Any]:
    kind, symbol, mint = _identity(arguments)
    body = {
        "directive_type": "chart",
        "directive_version": _DIRECTIVE_VERSION,
        "kind": kind,
        "symbol": symbol,
        "mint": mint,
        "timeframe": arguments["timeframe"],
        "title": arguments.get("title"),
        "chart_id": arguments.get("chart_id"),
        "price_scale": arguments.get("price_scale", "linear"),
        "visible_bars": arguments.get("visible_bars"),
    }
    body["directive_id"] = _directive_id(body)
    return {
        "directive_type": body["directive_type"],
        "directive_version": body["directive_version"],
        "directive_id": body["directive_id"],
        "kind": body["kind"],
        "symbol": body["symbol"],
        "mint": body["mint"],
        "timeframe": body["timeframe"],
        "title": body["title"],
        "chart_id": body["chart_id"],
        "price_scale": body["price_scale"],
        "visible_bars": body["visible_bars"],
    }


def add_indicator(
    arguments: Mapping[str, Any],
    _user: CurrentUser,
    _runtime: ToolRuntime,
) -> dict[str, Any]:
    name = str(arguments["indicator"])
    params = _params(name, arguments["params"])
    source = str(arguments.get("source", "close"))
    if source not in BUILTIN_SERIES:
        raise ToolError("validation_error", "source is not a builtin series", path="$.source")
    pane = arguments.get("pane", _PANES[name])
    color = arguments.get("color")
    if isinstance(color, str):
        color = color.lower()
    body = {
        "directive_type": "indicator",
        "directive_version": _DIRECTIVE_VERSION,
        "indicator": name,
        "params": params,
        "source": source,
        "outputs": list(_OUTPUTS[name]),
        "pane": pane,
        "warmup_bars": _warmup(name, params),
        "chart_id": arguments.get("chart_id"),
        "label": arguments.get("label"),
        "color": color,
    }
    body["directive_id"] = _directive_id(body)
    return {
        "directive_type": body["directive_type"],
        "directive_version": body["directive_version"],
        "directive_id": body["directive_id"],
        "indicator": body["indicator"],
        "params": body["params"],
        "source": body["source"],
        "outputs": body["outputs"],
        "pane": body["pane"],
        "warmup_bars": body["warmup_bars"],
        "chart_id": body["chart_id"],
        "label": body["label"],
        "color": body["color"],
    }


def _identity(arguments: Mapping[str, Any]) -> tuple[str, str, str]:
    if "symbol" in arguments:
        return _symbol(str(arguments["symbol"]), "$.symbol")
    if "mint" in arguments:
        return _mint(str(arguments["mint"]), "$.mint")
    return _asset(str(arguments["asset"]))


def _symbol(value: str, path: str) -> tuple[str, str, str]:
    text = value.strip()
    asset = BY_SYMBOL.get(text.upper())
    if asset is None:
        raise ToolError("unknown_symbol", "unknown symbol", path=path)
    return "major", asset.symbol, asset.mint


def _mint(value: str, path: str) -> tuple[str, str, str]:
    text = value.strip()
    try:
        mint = require_mint(text)
    except InvalidMint:
        raise ToolError("invalid_mint", "invalid mint", path=path) from None
    major = BY_MINT.get(mint)
    if major is not None:
        return "major", major.symbol, major.mint
    return "mint", mint, mint


def _asset(value: str) -> tuple[str, str, str]:
    text = value.strip()
    if text == "":
        raise ToolError("validation_error", "asset is empty", path="$.asset")
    asset = BY_SYMBOL.get(text.upper())
    if asset is not None:
        return "major", asset.symbol, asset.mint
    try:
        mint = require_mint(text)
    except InvalidMint:
        if text.isalpha() and len(text) <= 16:
            raise ToolError("unknown_symbol", "unknown symbol", path="$.asset") from None
        raise ToolError("invalid_mint", "invalid mint", path="$.asset") from None
    major = BY_MINT.get(mint)
    if major is not None:
        return "major", major.symbol, major.mint
    return "mint", mint, mint


def _params(name: str, raw: Mapping[str, Any]) -> dict[str, int | float]:
    expected = INDICATOR_PARAMS[name]
    if set(raw) != set(expected):
        raise ToolError("validation_error", "params do not match the indicator", path="$.params")
    ordered: dict[str, int | float] = {}
    for key in expected:
        ordered[key] = _param(name, key, raw[key])
    return ordered


def _param(name: str, key: str, value: object) -> int | float:
    path = f"$.params.{key}"
    if key == "stddev":
        try:
            band_width(value)
        except IndicatorError as exc:
            raise ToolError(
                "validation_error",
                safe_text(exc, "stddev is not valid"),
                path=path,
            ) from None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError("validation_error", "stddev is not valid", path=path)
        return value
    minimum = 2 if name == "rsi" and key == "period" else 1
    try:
        period = as_period(value, key)
    except IndicatorError as exc:
        raise ToolError(
            "validation_error",
            safe_text(exc, f"{key} is not valid"),
            path=path,
        ) from None
    if period < minimum:
        raise ToolError(
            "validation_error",
            f"{key} must be an integer from {minimum} to 500",
            path=path,
        )
    return period


def _warmup(name: str, params: Mapping[str, int | float]) -> int:
    if name == "sma":
        return sma_warmup(int(params["period"]))
    if name == "ema":
        return ema_warmup(int(params["period"]))
    if name == "atr":
        return atr_warmup(int(params["period"]))
    if name == "rsi":
        return rsi_warmup(int(params["period"]))
    if name == "macd":
        return max(macd_warmup(int(params["fast"]), int(params["slow"]), int(params["signal"])))
    if name == "bollinger":
        return bollinger_warmup(int(params["period"]))
    return max(stochastic_warmup(int(params["k_period"]), int(params["d_period"])))


def _directive_id(body: Mapping[str, Any]) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
