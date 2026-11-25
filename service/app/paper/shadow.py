"""Quote-only shadow next to a paper fill.

When ``SHADOW_QUOTES_ENABLED`` is the exact string ``1``, a paper fill is
followed by one ``ExecutionVenue.quote`` for the trade the paper book just
simulated. ``SHADOW_BUILD_ENABLED`` ``1`` also asks for an unsigned build.
Nothing here signs or sends. ``execute`` is not called.

The row stores the quoted output, the price impact, the route, the fee
lines, and the quote latency. Network fee, priority fee, and token-account
rent stay on this row. They are not added to strategy P&L. The provider
body is not stored. Rows purge at 24 hours with the other market data.

``slippageBps`` is a request parameter (``SHADOW_SLIPPAGE_BPS``, default
50). The risk engine owns the cap in P4-04. This module does not reject a
quote for being outside a risk limit.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from app.ohlcv.registry import BONK_MINT, BY_SYMBOL, JTO_MINT, JUP_MINT, SOL_MINT, WIF_MINT
from app.ohlcv.store import SqliteBarStore
from app.venue.address import require_address
from app.venue.errors import VenueError, VenueRejected
from app.venue.jupiter import JupiterVenue
from app.venue.pace import (
    DEFAULT_DAILY_CAP,
    DEFAULT_MIN_INTERVAL_SECONDS,
    daily_cap,
    min_interval_seconds,
)
from app.venue.types import QuoteRequest

logger = logging.getLogger("app.paper.shadow")

# USDC is the paper book's quote currency. This is the public mint, not a key.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# SPL decimals for the majors and USDC. This phase does not read mint accounts.
_DECIMALS = {
    SOL_MINT: 9,
    USDC_MINT: 6,
    JUP_MINT: 6,
    JTO_MINT: 9,
    BONK_MINT: 5,
    WIF_MINT: 6,
}
DEFAULT_SHADOW_SLIPPAGE_BPS = 50
_PAGE = 50

_shared_lock = threading.Lock()
_shared_venue: JupiterVenue | None = None


def shadow_quotes_enabled(raw: str | None = None) -> bool:
    """True only for the exact string ``1``. Unset and every other value are off."""
    if raw is None:
        raw = os.environ.get("SHADOW_QUOTES_ENABLED")
        if raw is None:
            return False
    return raw == "1"


def shadow_build_enabled(raw: str | None = None) -> bool:
    """True only for the exact string ``1``. The build is optional."""
    if raw is None:
        raw = os.environ.get("SHADOW_BUILD_ENABLED")
        if raw is None:
            return False
    return raw == "1"


def shadow_slippage_bps(raw: str | None = None) -> int:
    """Request ``slippageBps``. Blank keeps 50. This is not a risk cap."""
    if raw is None:
        raw = os.environ.get("SHADOW_SLIPPAGE_BPS")
        if raw is None:
            return DEFAULT_SHADOW_SLIPPAGE_BPS
    text = raw.strip()
    if text == "":
        return DEFAULT_SHADOW_SLIPPAGE_BPS
    if not text.isascii() or not text.isdigit() or (len(text) > 1 and text.startswith("0")):
        raise ValueError("bad_slippage")
    value = int(text)
    if value > 10_000:
        raise ValueError("bad_slippage")
    return value


def observe_shadow(
    store: SqliteBarStore,
    *,
    user_id: str,
    strategy_id: str,
    version: int,
    document: Mapping[str, Any],
    now: int,
    venue: object | None = None,
    chain_rpc: object | None = None,
) -> None:
    """Quote fills that do not yet have a shadow row. The paper book is not written.

    The flag is read here. Off does no venue work. A venue error is stored
    as a short code. The provider body is not.
    """
    if not shadow_quotes_enabled():
        return
    slippage = shadow_slippage_bps()
    pending = store.unshadowed_paper_fills(
        user_id=user_id, strategy_id=strategy_id, version=version
    )
    if not pending:
        return
    client = venue if venue is not None else _venue()
    for fill in pending:
        try:
            _one(
                store,
                client,
                user_id=user_id,
                strategy_id=strategy_id,
                version=version,
                document=document,
                fill=fill,
                now=now,
                slippage=slippage,
                chain_rpc=chain_rpc,
            )
        except Exception as exc:
            logger.warning(
                "shadow quote failed strategy_id=%s version=%s code=%s",
                strategy_id,
                version,
                _code(exc),
            )


def shadow_report(store: SqliteBarStore, user_id: str) -> dict[str, Any]:
    """The dashboard body. Computed from rows still inside the 24-hour purge."""
    try:
        slippage: int | None = shadow_slippage_bps()
    except ValueError:
        slippage = None
    try:
        interval: float | None = min_interval_seconds()
        cap: int | None = daily_cap()
    except VenueError:
        interval = None
        cap = None
    attempts, mismatches = store.shadow_build_counts(user_id=user_id)
    rate = None
    if attempts > 0:
        ratio = (Decimal(mismatches) / Decimal(attempts)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )
        rate = format(ratio, "f")
    quotes = [
        _public(row) for row in store.list_paper_shadow(user_id=user_id, limit=_PAGE)
    ]
    return {
        "enabled": shadow_quotes_enabled(),
        "build_enabled": shadow_build_enabled(),
        "slippage_bps": slippage,
        "min_interval_seconds": None if interval is None else _seconds(interval),
        "daily_cap": cap,
        "amount_mismatch_rate": rate,
        "build_attempts": attempts,
        "amount_mismatches": mismatches,
        "quotes": quotes,
    }


def _one(
    store: SqliteBarStore,
    venue: object,
    *,
    user_id: str,
    strategy_id: str,
    version: int,
    document: Mapping[str, Any],
    fill: tuple[object, ...],
    now: int,
    slippage: int,
    chain_rpc: object | None = None,
) -> None:
    fill_id, open_time, asset_id, side, qty, notional = fill
    base = {
        "user_id": user_id,
        "strategy_id": strategy_id,
        "version": version,
        "fill_id": fill_id,
        "open_time": open_time,
        "asset_id": asset_id,
        "side": side,
        "input_mint": None,
        "output_mint": None,
        "paper_out": str(qty if side == "buy" else notional),
        "quoted_out": None,
        "price_impact": None,
        "route_label": None,
        "router": None,
        "network_fee_lamports": None,
        "priority_fee_lamports": None,
        "rent_lamports": None,
        "latency_ms": None,
        "slippage_bps": slippage,
        "quote_status": "error",
        "build_status": "not_requested",
        "error_code": None,
        "quoted_at": now,
    }
    pair = _pair(document, str(asset_id), str(side), str(qty), str(notional))
    if isinstance(pair, str):
        base["quote_status"] = pair
        base["error_code"] = pair
        store.insert_paper_shadow(base)
        return
    input_mint, output_mint, amount, paper_out, out_decimals = pair
    base["input_mint"] = input_mint
    base["output_mint"] = output_mint
    base["paper_out"] = paper_out
    if not store.take_shadow_call(now=now, cap=_cap()):
        return
    request = QuoteRequest(input_mint, output_mint, amount, str(side), slippage)
    started = time.perf_counter()
    try:
        quote = venue.quote(request, now=now)  # type: ignore[attr-defined]
    except Exception as exc:
        base["latency_ms"] = _millis(started)
        base["quote_status"] = "error"
        base["error_code"] = _code(exc)
        if _code(exc) != "quote_cap":
            store.insert_paper_shadow(base)
        return
    base["latency_ms"] = _millis(started)
    base["quote_status"] = "ok"
    base["quoted_out"] = _human(int(quote.out_amount), out_decimals)
    base["price_impact"] = str(quote.price_impact)
    base["route_label"] = str(quote.route_label)
    base["router"] = str(quote.router)
    base["network_fee_lamports"] = quote.network_fee_lamports
    base["priority_fee_lamports"] = quote.priority_fee_lamports
    base["rent_lamports"] = quote.rent_fee_lamports
    _maybe_build(store, venue, quote, base, now=now, slippage=slippage, chain_rpc=chain_rpc)
    store.insert_paper_shadow(base)


def _maybe_build(
    store: SqliteBarStore,
    venue: object,
    quote: object,
    base: dict[str, object],
    *,
    now: int,
    slippage: int,
    chain_rpc: object | None = None,
) -> None:
    if not shadow_build_enabled():
        base["build_status"] = "not_requested"
        return
    taker = _taker()
    # The quote's lastValidBlockHeight is the venue expiry, not the chain tip.
    height = _chain_height(chain_rpc)
    if taker is None:
        base["build_status"] = "skipped"
        base["error_code"] = "no_taker"
        return
    if not isinstance(height, int):
        base["build_status"] = "skipped"
        base["error_code"] = "missing_block_height"
        return
    if not store.take_shadow_call(now=now, cap=_cap()):
        base["build_status"] = "capped"
        return
    try:
        venue.build(  # type: ignore[attr-defined]
            quote,
            taker=taker,
            now=now,
            block_height=height,
            slippage_bps=slippage,
        )
    except VenueRejected as exc:
        base["build_status"] = "amount_mismatch" if exc.code == "amount_mismatch" else "rejected"
        base["error_code"] = exc.code
        return
    except Exception as exc:
        base["build_status"] = "error"
        base["error_code"] = _code(exc)
        return
    base["build_status"] = "ok"


def _pair(
    document: Mapping[str, Any],
    asset_id: str,
    side: str,
    qty: str,
    notional: str,
) -> tuple[str, str, int, str, int] | str:
    """The trade, or ``unsupported_mint`` / ``bad_amount`` when it cannot be quoted."""
    portfolio = document.get("portfolio")
    if not isinstance(portfolio, Mapping) or portfolio.get("quote_currency") != "USDC":
        return "unsupported_mint"
    assets = document.get("assets")
    if not isinstance(assets, list):
        return "unsupported_mint"
    mint = None
    for asset in assets:
        if isinstance(asset, Mapping) and asset.get("id") == asset_id:
            known = BY_SYMBOL.get(str(asset.get("symbol")))
            mint = None if known is None else known.mint
            break
    if mint is None or mint not in _DECIMALS:
        return "unsupported_mint"
    try:
        if side == "buy":
            amount = _atoms(notional, _DECIMALS[USDC_MINT])
            return USDC_MINT, mint, amount, _plain(qty), _DECIMALS[mint]
        if side == "sell":
            amount = _atoms(qty, _DECIMALS[mint])
            return mint, USDC_MINT, amount, _plain(notional), _DECIMALS[USDC_MINT]
    except (ValueError, ArithmeticError):
        return "bad_amount"
    return "unsupported_mint"


def _atoms(text: str, decimals: int) -> int:
    amount = Decimal(text)
    if not amount.is_finite() or amount <= 0:
        raise ValueError("bad_amount")
    scaled = (amount * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_HALF_EVEN)
    if scaled <= 0 or scaled >= 2**64:
        raise ValueError("bad_amount")
    return int(scaled)


def _human(atoms: int, decimals: int) -> str:
    text = format(Decimal(atoms) / (Decimal(10) ** decimals), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _seconds(value: float) -> str:
    text = format(value, ".6f").rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _plain(text: str) -> str:
    amount = Decimal(text)
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _chain_height(chain_rpc: object | None) -> int | None:
    """Block height from an RPC interface. A quote field is not accepted."""
    from app.risk.rpc import chain_block_height, default_rpc

    rpc = chain_rpc if chain_rpc is not None else default_rpc()
    if rpc is None:
        return None
    try:
        return chain_block_height(rpc)
    except Exception:
        return None


def _taker() -> str | None:
    raw = os.environ.get("SHADOW_TAKER")
    if raw is None or raw.strip() == "":
        return None
    try:
        return require_address(raw.strip())
    except VenueError:
        return None


def _cap() -> int:
    try:
        return daily_cap()
    except VenueError:
        return DEFAULT_DAILY_CAP


def _venue() -> JupiterVenue:
    global _shared_venue
    with _shared_lock:
        if _shared_venue is None:
            _shared_venue = JupiterVenue()
        return _shared_venue


def _millis(started: float) -> int:
    elapsed = (time.perf_counter() - started) * 1000
    if elapsed < 0:
        return 0
    if elapsed > 3_600_000:
        return 3_600_000
    return int(elapsed)


def _code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _safe_code(code):
        return code
    return "error"


def _safe_code(code: str) -> bool:
    if len(code) > 64 or not code.isascii():
        return False
    return all(char.isalnum() or char == "_" for char in code)


def _public(row: tuple[object, ...]) -> dict[str, object]:
    (
        strategy_id,
        version,
        open_time,
        asset_id,
        side,
        input_mint,
        output_mint,
        paper_out,
        quoted_out,
        price_impact,
        route_label,
        router,
        network_fee,
        priority_fee,
        rent_fee,
        latency_ms,
        slippage_bps,
        quote_status,
        build_status,
        error_code,
        quoted_at,
    ) = row
    return {
        "strategy_id": strategy_id,
        "version": version,
        "open_time": open_time,
        "asset_id": asset_id,
        "side": side,
        "input_mint": input_mint,
        "output_mint": output_mint,
        "paper_out": paper_out,
        "quoted_out": quoted_out,
        "price_impact": price_impact,
        "route_label": route_label,
        "router": router,
        "network_fee_lamports": network_fee,
        "priority_fee_lamports": priority_fee,
        "rent_lamports": rent_fee,
        "latency_ms": latency_ms,
        "slippage_bps": slippage_bps,
        "quote_status": quote_status,
        "build_status": build_status,
        "error_code": error_code,
        "quoted_at": quoted_at,
    }


# Re-exported so a reader can see the throttle next to the flag.
__all__ = [
    "DEFAULT_DAILY_CAP",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "DEFAULT_SHADOW_SLIPPAGE_BPS",
    "observe_shadow",
    "shadow_build_enabled",
    "shadow_quotes_enabled",
    "shadow_report",
    "shadow_slippage_bps",
]
