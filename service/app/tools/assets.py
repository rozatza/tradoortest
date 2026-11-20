"""``search_asset`` wraps the registry and ROZ-92 pool resolution.

Known majors never call the network. An arbitrary mint uses
``AnyMintSource.resolve_mint`` and the 24-hour ``pool_resolutions`` row.
This tool does not read or write OHLCV bars.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.auth import CurrentUser
from app.ohlcv.errors import InvalidMint
from app.ohlcv.mint import require_mint
from app.ohlcv.pools import ChosenPool
from app.ohlcv.registry import BY_MINT, BY_SYMBOL, MAJORS, Asset
from app.tools.errors import ToolError
from app.tools.runtime import ToolRuntime

_SYMBOL = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def search_asset(
    arguments: Mapping[str, Any],
    user: CurrentUser,
    runtime: ToolRuntime,
) -> dict[str, Any]:
    query = str(arguments["query"]).strip()
    if query == "":
        raise ToolError("validation_error", "query is empty", path="$.query")
    limit = int(arguments.get("limit", 10))
    exact = BY_SYMBOL.get(query.upper())
    if exact is not None and query.upper() == exact.symbol:
        return _payload(query, [_major(exact)])
    mint = _as_mint(query)
    if mint is not None:
        major = BY_MINT.get(mint)
        if major is not None:
            return _payload(query, [_major(major)])
        return _payload(query, [_resolve_pool(runtime, user, mint)])
    matches = [asset for asset in MAJORS if query.upper() in asset.symbol]
    if matches:
        return _payload(query, [_major(asset) for asset in matches[:limit]])
    if _symbol_text(query):
        return _payload(query, [])
    raise ToolError("invalid_mint", "invalid mint", path="$.query")


def _payload(query: str, matches: list[dict[str, Any]]) -> dict[str, Any]:
    return {"query": query, "matches": matches}


def _major(asset: Asset) -> dict[str, Any]:
    return {
        "kind": "major",
        "symbol": asset.symbol,
        "mint": asset.mint,
        "pool": None,
        "cached": False,
        "provider": None,
    }


def _as_mint(query: str) -> str | None:
    try:
        return require_mint(query)
    except InvalidMint:
        return None


def _symbol_text(query: str) -> bool:
    return 1 <= len(query) <= 16 and query[0].isalpha() and all(char in _SYMBOL for char in query)


def _resolve_pool(runtime: ToolRuntime, user: CurrentUser, mint: str) -> dict[str, Any]:
    ohlcv = runtime.ohlcv()
    cached = ohlcv.store.load_pool(user_id=user.user_id, mint=mint)
    if cached is not None:
        return _from_row(mint, cached, cached=True)
    source = ohlcv.source
    resolve = getattr(source, "resolve_mint", None)
    if resolve is None:
        raise ToolError(
            "service_unavailable",
            "the asset source is unavailable",
            retryable=True,
        )
    chosen = resolve(mint)
    if not isinstance(chosen, ChosenPool):
        raise ToolError(
            "service_unavailable",
            "the asset source is unavailable",
            retryable=True,
        )
    provider = _provider_name(source)
    ohlcv.store.save_pool(
        user_id=user.user_id,
        mint=mint,
        address=chosen.address,
        dex=chosen.dex,
        dex_id=chosen.dex_id,
        quote_token=chosen.quote_token,
        quote_symbol=chosen.quote_symbol,
        liquidity_usd=chosen.liquidity_usd,
        resolved_at=chosen.resolved_at,
        pool_created_at=chosen.pool_created_at,
        provider=provider,
    )
    return _from_pool(mint, chosen, provider=provider, cached=False)


def _provider_name(source: object) -> str:
    """Provider that resolved the pool, or the GeckoTerminal default."""
    method = getattr(source, "pool_provider", None)
    if callable(method):
        found = method()
        if isinstance(found, str) and found != "" and len(found) <= 32 and "\x00" not in found:
            return found
    return "geckoterminal"


def _from_row(mint: str, row: Mapping[str, object], *, cached: bool) -> dict[str, Any]:
    resolved = row.get("resolved_at")
    created = row.get("pool_created_at")
    symbol = row.get("quote_symbol")
    if not isinstance(resolved, int):
        raise ToolError("service_unavailable", "the local store is unavailable", retryable=True)
    pool = ChosenPool(
        address=_text(row.get("address")),
        dex=_text(row.get("dex")),
        dex_id=_text(row.get("dex_id")),
        quote_token=_text(row.get("quote_token")),
        quote_symbol=None if symbol is None else _text(symbol),
        liquidity_usd=_text(row.get("liquidity_usd")),
        resolved_at=resolved,
        pool_created_at=created if isinstance(created, int) else None,
    )
    provider = row.get("provider")
    name = "geckoterminal" if not isinstance(provider, str) or provider == "" else provider
    if len(name) > 32:
        name = "geckoterminal"
    return _from_pool(mint, pool, provider=name, cached=cached)


def _from_pool(mint: str, pool: ChosenPool, *, provider: str, cached: bool) -> dict[str, Any]:
    body = dict(pool.public())
    body["pool_created_at"] = pool.pool_created_at
    return {
        "kind": "pool",
        "symbol": mint,
        "mint": mint,
        "pool": body,
        "cached": cached,
        "provider": provider,
    }


def _text(value: object) -> str:
    if not isinstance(value, str) or value == "" or len(value) > 128 or "\x00" in value:
        raise ToolError("service_unavailable", "the local store is unavailable", retryable=True)
    return value
