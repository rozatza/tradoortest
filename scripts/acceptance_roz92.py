#!/usr/bin/env python3
"""Live any-mint check. Not part of pytest and not run by CI.

Discovers three non-major Solana mints on the public GeckoTerminal API
(a liquid pool, a sparse pool, and a fresh pool), ingests one day of
5-minute bars for each, and prints the pool the any-mint source chose.
The SQLite file is created in a temporary directory and deleted before
the process exits. Birdeye is forced off. Nothing is written under data/.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from app.ohlcv.adapters.any_mint import AnyMintSource  # noqa: E402
from app.ohlcv.backfill import plan_window  # noqa: E402
from app.ohlcv.errors import SourceError  # noqa: E402
from app.ohlcv.ingest import run_mint_ingest  # noqa: E402
from app.ohlcv.mint import require_mint  # noqa: E402
from app.ohlcv.registry import (  # noqa: E402
    BONK_MINT,
    JTO_MINT,
    JUP_MINT,
    SOL_MINT,
    WIF_MINT,
    Asset,
)
from app.ohlcv.store import SqliteBarStore  # noqa: E402

_BASE = "https://api.geckoterminal.com/api/v2"
_GAP = 12.0
_EXCLUDED = {
    SOL_MINT,
    JUP_MINT,
    JTO_MINT,
    BONK_MINT,
    WIF_MINT,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
_BONDING = frozenset({"pump-fun", "pumpfun", "pump.fun"})
_USER = "local-user"


def main() -> int:
    rows: list[dict[str, object]] = []
    client = httpx.Client(
        timeout=30.0,
        headers={"Accept": "application/json", "User-Agent": "tradoortest-ohlcv"},
    )
    directory = tempfile.TemporaryDirectory(prefix="roz92-accept-")
    try:
        found = _discover(client)
        for kind, candidates in found.items():
            row = _ingest(kind, candidates, Path(directory.name))
            rows.append(row)
    finally:
        client.close()
        directory.cleanup()
    print(json.dumps(rows, indent=2))
    print()
    print("| class | mint | pool | dex | quote | bars | flags |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        flags = ", ".join(row["flags"]) if isinstance(row["flags"], list) else ""
        print(
            "| {kind} | `{mint}` | `{pool}` | {dex} | `{quote}` | {bars} | {flags} |".format(
                kind=row["class"],
                mint=row["mint"],
                pool=row["pool"],
                dex=row["dex"],
                quote=row["quote"],
                bars=row["bars"],
                flags=flags or "none",
            )
        )
    failed = [row for row in rows if row.get("error")]
    return 1 if failed or len(rows) < 3 else 0


def _discover(client: httpx.Client) -> dict[str, list[str]]:
    """Return candidate mints for liquid, sparse, and fresh. Order is preference."""
    top = _pools(client, f"{_BASE}/networks/solana/pools", page=1)
    fresh_pages = [
        _pools(client, f"{_BASE}/networks/solana/new_pools", page=page) for page in (1, 2)
    ]
    now = time.time()
    liquid = [
        item
        for item in top
        if not item["bonding"] and item["reserve"] >= Decimal("50000")
    ]
    liquid.sort(key=lambda item: item["reserve"], reverse=True)
    newest: list[dict[str, Any]] = []
    thin: list[dict[str, Any]] = []
    for page in fresh_pages:
        for item in page:
            if item["bonding"]:
                continue
            age = None if item["created"] is None else now - item["created"]
            if age is not None and age <= 3 * 86400:
                newest.append(item)
            if Decimal("0") < item["reserve"] < Decimal("20000"):
                thin.append(item)
    newest.sort(key=lambda item: item["created"] if item["created"] is not None else 0, reverse=True)
    thin.sort(key=lambda item: item["reserve"])
    if not thin:
        # A later top-pool page is still deep. New pools are the thin book.
        extra = _pools(client, f"{_BASE}/networks/solana/new_pools", page=3)
        thin = [
            item
            for item in extra
            if not item["bonding"] and Decimal("0") < item["reserve"] < Decimal("50000")
        ]
        thin.sort(key=lambda item: item["reserve"])
    # One primary each, disjoint, before backups. The thin pools are also the
    # newest ones, so filling the fresh list first would leave sparse empty.
    used: set[str] = set()
    liquid_mints = _take(liquid, used, 1)
    fresh_source = newest or [item for item in fresh_pages[0] if not item["bonding"]]
    fresh_mints = _take(fresh_source, used, 1)
    sparse_mints = _take(thin, used, 1)
    liquid_mints += _take(liquid, used, 2)
    fresh_mints += _take(fresh_source, used, 2)
    sparse_mints += _take(thin, used, 2)
    picked = {"liquid": liquid_mints, "fresh": fresh_mints, "sparse": sparse_mints}
    missing = [kind for kind, mints in picked.items() if not mints]
    if missing:
        raise SystemExit(f"could not find a {', '.join(missing)} mint on GeckoTerminal")
    return picked


def _take(items: list[dict[str, Any]], used: set[str], limit: int) -> list[str]:
    found: list[str] = []
    for item in items:
        mint = item["mint"]
        if mint in used:
            continue
        used.add(mint)
        found.append(mint)
        if len(found) >= limit:
            break
    return found


def _ingest(kind: str, mints: list[str], data_dir: Path) -> dict[str, object]:
    last_error = "no candidate could be ingested"
    for mint in mints:
        source = AnyMintSource(api_key=None, now=lambda: int(time.time()))
        store = SqliteBarStore(data_dir / f"{kind}.sqlite")
        try:
            clock = int(time.time())
            start, end = plan_window(clock, 1, ("5m",))
            report = run_mint_ingest(
                source=source,
                store=store,
                assets=(Asset(mint, mint),),
                start=start,
                end=end,
                timeframes=("5m",),
                user_id=_USER,
                generated_at=clock,
            )
        except (SourceError, ValueError, OSError) as exc:
            last_error = str(exc).splitlines()[0]
            continue
        finally:
            store.close()
            source.close()
        series = report["series"][0]
        pool = series.get("pool") if isinstance(series, dict) else None
        if not isinstance(pool, dict):
            last_error = "ingest did not record a pool"
            continue
        flags = series.get("flags")
        return {
            "class": kind,
            "mint": mint,
            "pool": pool.get("address"),
            "dex": pool.get("dex"),
            "quote": pool.get("quote_token"),
            "bars": series.get("real"),
            "flags": flags if isinstance(flags, list) else [],
            "error": None,
        }
    return {
        "class": kind,
        "mint": None,
        "pool": None,
        "dex": None,
        "quote": None,
        "bars": None,
        "flags": [],
        "error": last_error,
    }


def _pools(client: httpx.Client, url: str, *, page: int) -> list[dict[str, Any]]:
    payload = _get(client, url, page)
    if not isinstance(payload, dict):
        return []
    included = _included(payload.get("included"))
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    parsed: list[dict[str, Any]] = []
    for row in rows:
        item = _one(row, included)
        if item is not None:
            parsed.append(item)
    return parsed


def _get(client: httpx.Client, url: str, page: int) -> dict[str, Any]:
    last_error = "GeckoTerminal discovery failed"
    for _attempt in range(5):
        _pause()
        response = client.get(url, params={"include": "base_token,quote_token,dex", "page": page})
        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"GeckoTerminal discovery returned HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise SystemExit(f"GeckoTerminal discovery returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SystemExit("GeckoTerminal discovery did not return an object")
        return payload
    raise SystemExit(last_error)


_last = 0.0


def _pause() -> None:
    global _last
    wait = _GAP - (time.monotonic() - _last)
    if _last and wait > 0:
        time.sleep(wait)
    _last = time.monotonic()


def _included(raw: object) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return found
    for row in raw:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            found[row["id"]] = row
    return found


def _one(row: object, included: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    attributes = row.get("attributes")
    if not isinstance(attributes, dict):
        return None
    reserve = _decimal(attributes.get("reserve_in_usd"))
    if reserve is None:
        return None
    dex_id = _dex_id(row)
    mint = _mint(row, included)
    if mint is None:
        return None
    details = attributes.get("launchpad_details")
    bonding = dex_id in _BONDING
    if isinstance(details, dict) and details.get("completed") is False:
        bonding = True
    return {
        "mint": mint,
        "reserve": reserve,
        "created": _created(attributes.get("pool_created_at")),
        "bonding": bonding,
        "dex_id": dex_id,
    }


def _mint(row: dict[str, Any], included: dict[str, dict[str, Any]]) -> str | None:
    for relation in ("base_token", "quote_token"):
        ident = _relation_id(row, relation)
        if ident is None:
            continue
        record = included.get(ident)
        attributes = record.get("attributes") if isinstance(record, dict) else None
        address = attributes.get("address") if isinstance(attributes, dict) else None
        text = address.strip() if isinstance(address, str) else ""
        if text == "" and ident.startswith("solana_"):
            text = ident.split("_", 1)[1]
        if text == "" or text in _EXCLUDED:
            continue
        try:
            return require_mint(text)
        except SourceError:
            continue
    return None


def _relation_id(row: dict[str, Any], name: str) -> str | None:
    relationships = row.get("relationships")
    data = relationships.get(name) if isinstance(relationships, dict) else None
    pointer = data.get("data") if isinstance(data, dict) else None
    ident = pointer.get("id") if isinstance(pointer, dict) else None
    if isinstance(ident, str) and ident.strip():
        return ident
    return None


def _dex_id(row: dict[str, Any]) -> str:
    ident = _relation_id(row, "dex")
    return ident or ""


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _created(value: object) -> float | None:
    if not isinstance(value, str) or value.strip() == "":
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.timestamp()


if __name__ == "__main__":
    sys.exit(main())
