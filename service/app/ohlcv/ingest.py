"""Any-mint ingest. The document is the CLI output and the job result.

Bars go through the same backfill and the same 24-hour cache as the
majors. This module adds the chosen pool, the provider that served each
series, and the data-quality flags. It does not back-fill a short history.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence

from app.ohlcv.backfill import run_backfill
from app.ohlcv.pools import ChosenPool
from app.ohlcv.quality import annotate_series
from app.ohlcv.registry import Asset
from app.ohlcv.source import OhlcvSource
from app.ohlcv.store import SqliteBarStore


def run_mint_ingest(
    *,
    source: OhlcvSource,
    store: SqliteBarStore,
    assets: Sequence[Asset],
    start: int,
    end: int,
    timeframes: Sequence[str],
    user_id: str,
    generated_at: int,
    progress: Callable[[str], None] | None = None,
    long_no_trade_minutes: int = 120,
    warmup_bars: int = 0,
    job_id: str | None = None,
) -> dict[str, object]:
    """Fetch ``assets`` and return the gap report plus pool and flags."""
    if isinstance(warmup_bars, bool) or not isinstance(warmup_bars, int) or warmup_bars < 0:
        raise ValueError("warmup bars must be a whole number, at least 0")
    starter = getattr(source, "begin_run", None)
    token = starter() if starter is not None else None
    try:
        return _mint_report(
            source=source,
            store=store,
            assets=assets,
            start=start,
            end=end,
            timeframes=timeframes,
            user_id=user_id,
            generated_at=generated_at,
            progress=progress,
            long_no_trade_minutes=long_no_trade_minutes,
            warmup_bars=warmup_bars,
            job_id=job_id,
        )
    finally:
        ender = getattr(source, "end_run", None)
        if ender is not None and token is not None:
            ender(token)


def _mint_report(
    *,
    source: OhlcvSource,
    store: SqliteBarStore,
    assets: Sequence[Asset],
    start: int,
    end: int,
    timeframes: Sequence[str],
    user_id: str,
    generated_at: int,
    progress: Callable[[str], None] | None,
    long_no_trade_minutes: int,
    warmup_bars: int,
    job_id: str | None,
) -> dict[str, object]:
    report = run_backfill(
        source=source,
        store=store,
        assets=assets,
        start=start,
        end=end,
        timeframes=timeframes,
        user_id=user_id,
        generated_at=generated_at,
        progress=progress,
        long_no_trade_minutes=long_no_trade_minutes,
    )
    pools: list[dict[str, object]] = []
    seen: set[str] = set()
    series = report.get("series")
    if not isinstance(series, list):
        raise ValueError("ingest report has no series")
    enriched: list[dict[str, object]] = []
    providers: list[str] = []
    for section in series:
        if not isinstance(section, dict):
            raise ValueError("ingest report has no series")
        mint = section.get("mint")
        timeframe = section.get("timeframe")
        if not isinstance(mint, str) or not isinstance(timeframe, str):
            raise ValueError("ingest report has no series")
        live = _pool(source, mint)
        pool = live if live is not None else _pool_from_store(store, user_id, mint)
        provider = _series_provider(store, source, user_id, mint, timeframe, start, end)
        providers.append(provider)
        if pool is not None and mint not in seen:
            seen.add(mint)
            stored = dict(pool.public())
            stored["mint"] = mint
            pools.append(stored)
            if live is not None:
                _save_pool(store, user_id=user_id, mint=mint, pool=pool, provider=provider)
        bars = store.load(
            user_id=user_id,
            mint=mint,
            timeframe=timeframe,
            start=start,
            end=end,
        )
        real = [bar for bar in bars if not bar.synthetic]
        enriched.append(
            annotate_series(
                section,
                real,
                pool=pool,
                provider=provider,
                warmup_bars=warmup_bars,
                window_start=start,
                window_end=end,
            )
        )
    report["series"] = enriched
    report["job_id"] = uuid.uuid4().hex if job_id is None else job_id
    report["pools"] = pools
    if providers and all(item == "birdeye" for item in providers):
        report["attribution"] = "Data by Birdeye"
    elif any(item in ("birdeye", "mixed") for item in providers):
        report["attribution"] = "Data by GeckoTerminal / CoinGecko and Birdeye"
    store.save_ingest_result(
        user_id=user_id,
        job_id=str(report["job_id"]),
        payload=report,
    )
    return report


def _pool(source: OhlcvSource, mint: str) -> ChosenPool | None:
    method = getattr(source, "pool_for", None)
    if method is None:
        return None
    found = method(mint)
    if found is None or isinstance(found, ChosenPool):
        return found
    return None


def _series_provider(
    store: SqliteBarStore,
    source: OhlcvSource,
    user_id: str,
    mint: str,
    timeframe: str,
    start: int,
    end: int,
) -> str:
    """The providers recorded on the spans, including after a process restart."""
    found = store.series_providers(
        user_id=user_id, mint=mint, timeframe=timeframe, start=start, end=end
    )
    if len(found) > 1:
        return "mixed"
    if len(found) == 1:
        return found[0]
    return _provider(source, mint, timeframe)


def _pool_from_store(store: SqliteBarStore, user_id: str, mint: str) -> ChosenPool | None:
    row = store.load_pool(user_id=user_id, mint=mint)
    if row is None:
        return None
    created = row.get("pool_created_at")
    symbol = row.get("quote_symbol")
    resolved = row.get("resolved_at")
    if not isinstance(resolved, int):
        return None
    return ChosenPool(
        address=str(row.get("address")),
        dex=str(row.get("dex")),
        dex_id=str(row.get("dex_id")),
        quote_token=str(row.get("quote_token")),
        quote_symbol=None if symbol is None else str(symbol),
        liquidity_usd=str(row.get("liquidity_usd")),
        resolved_at=resolved,
        pool_created_at=created if isinstance(created, int) else None,
    )


def _provider(source: OhlcvSource, mint: str, timeframe: str) -> str:
    method = getattr(source, "served_provider", None)
    if method is None:
        return source.name
    found = method(mint, timeframe)
    if isinstance(found, str) and found != "":
        return found
    return source.name


def _save_pool(
    store: SqliteBarStore,
    *,
    user_id: str,
    mint: str,
    pool: ChosenPool,
    provider: str,
) -> None:
    store.save_pool(
        user_id=user_id,
        mint=mint,
        address=pool.address,
        dex=pool.dex,
        dex_id=pool.dex_id,
        quote_token=pool.quote_token,
        quote_symbol=pool.quote_symbol,
        liquidity_usd=pool.liquidity_usd,
        resolved_at=pool.resolved_at,
        pool_created_at=pool.pool_created_at,
        provider=provider,
    )
