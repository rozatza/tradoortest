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
        pool = _pool(source, mint)
        provider = _provider(source, mint, timeframe)
        providers.append(provider)
        if pool is not None and mint not in seen:
            seen.add(mint)
            stored = dict(pool.public())
            stored["mint"] = mint
            pools.append(stored)
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
        quote_token=pool.quote_token,
        liquidity_usd=pool.liquidity_usd,
        resolved_at=pool.resolved_at,
        pool_created_at=pool.pool_created_at,
        provider=provider,
    )
