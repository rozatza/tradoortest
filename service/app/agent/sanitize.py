"""Strip market data before it is written to ``agent.sqlite``.

OHLCV bars stay in the backtest job body. Backtest results stay in the
24-hour job result. Pool liquidity stays in the 24-hour pool cache. The
agent store keeps ids, counts, and static symbol or mint names.
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.config import MAX_STORED_CHARS, secret_values

_DROP_KEYS = frozenset(
    {
        "bars",
        "pool",
        "liquidity_usd",
        "reserve_in_usd",
        "quote_token",
        "dex_id",
        "pool_created_at",
        "resolved_at",
        "fills",
        "trades",
        "final_equity",
        "final_cash",
        "equity_after",
        "equity_curve",
    }
)
_BACKTEST_KEEP = ("job_id", "status", "ready", "created_at", "expires_at", "error")
_ARG_KEEP = ("strategy_id", "version", "request_key")


def sanitize_tool_payload(tool: str, kind: str, payload: object) -> object:
    """Return a JSON value that is safe to store for this tool call."""
    if tool == "backtest_start" and kind == "arguments":
        return _scrub(_backtest_arguments(payload))
    if tool == "backtest_get" and kind == "result":
        return _scrub(_backtest_result(payload))
    if tool == "search_asset" and kind == "result":
        return _scrub(_search_result(payload))
    return _scrub(payload)


def dumps_stored(payload: object, *, limit: int = MAX_STORED_CHARS) -> str:
    """JSON for a message or event, redacted and size-capped."""
    try:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = "{}"
    text = redact_secrets(text)
    if len(text) <= limit:
        return text
    return json.dumps({"truncated": True}, separators=(",", ":"))


def redact_secrets(text: str) -> str:
    """Replace configured API key values. The keys themselves are not logged."""
    redacted = text
    for secret in secret_values():
        if len(secret) >= 8 and secret in redacted:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _backtest_arguments(payload: object) -> dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    bars = body.get("bars")
    symbols: list[str] = []
    count = 0
    if isinstance(bars, dict):
        symbols = sorted(key for key in bars if isinstance(key, str))
        for rows in bars.values():
            if isinstance(rows, list):
                count += len(rows)
    kept: dict[str, Any] = {
        "bar_count": count,
        "symbols": symbols,
        "bars_omitted": True,
    }
    for key in _ARG_KEEP:
        if key in body:
            kept[key] = body[key]
    return kept


def _backtest_result(payload: object) -> object:
    if not isinstance(payload, dict):
        return {"result_omitted": True}
    if "ok" in payload and "data" in payload:
        if payload.get("ok") is not True:
            return _error_envelope(payload)
        data = payload.get("data")
        return {
            "ok": True,
            "tool": "backtest_get",
            "data": _backtest_data(data),
        }
    return _backtest_data(payload)


def _backtest_data(data: object) -> dict[str, Any]:
    kept: dict[str, Any] = {"result_omitted": True}
    if not isinstance(data, dict):
        return kept
    for key in _BACKTEST_KEEP:
        if key in data:
            kept[key] = data[key]
    return kept


def _search_result(payload: object) -> object:
    if not isinstance(payload, dict):
        return {"query": None, "match_count": 0, "matches": [], "provider_data_omitted": True}
    if "ok" in payload and "data" in payload:
        if payload.get("ok") is not True:
            return _error_envelope(payload)
        return {
            "ok": True,
            "tool": "search_asset",
            "data": _search_data(payload.get("data")),
        }
    return _search_data(payload)


def _search_data(data: object) -> dict[str, Any]:
    query = None
    matches: list[dict[str, Any]] = []
    if isinstance(data, dict):
        query = data.get("query")
        raw = data.get("matches")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                matches.append(
                    {
                        "kind": item.get("kind"),
                        "symbol": item.get("symbol"),
                        "mint": item.get("mint"),
                        "provider_data_omitted": True,
                    }
                )
    return {
        "query": query,
        "match_count": len(matches),
        "matches": matches,
        "provider_data_omitted": True,
    }


def _error_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error")
    if not isinstance(error, dict):
        return {"ok": False, "tool": payload.get("tool"), "error": {"code": "internal_error"}}
    return {
        "ok": False,
        "tool": payload.get("tool"),
        "error": {
            "code": error.get("code"),
            "message": error.get("message"),
            "path": error.get("path"),
            "retryable": error.get("retryable"),
        },
    }


def _scrub(value: object) -> object:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in _DROP_KEYS or key == "result":
                continue
            cleaned[key] = _scrub(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value
