"""Read the paper book for the dashboard.

The body is stored rows only. Realized P&L is the position's ``realized``
text. A mark, when one exists, is the newest stored close that is not
synthetic, labeled ``mark``. This module does not price a wallet and it
does not invent a fill.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any

from app.ohlcv.bars import TIMEFRAME_SECONDS
from app.ohlcv.registry import BY_SYMBOL
from app.ohlcv.store import SqliteBarStore
from app.strategies.errors import StrategyNotFound, VersionNotFound
from app.strategies.store import StrategyStore


def paper_snapshot(
    bars: SqliteBarStore,
    strategies: StrategyStore,
    user_id: str,
    *,
    events_since: int | None,
    events_limit: int,
) -> dict[str, Any]:
    """Running registrations, position rows, and one page of the event log.

    ``events_since`` ``None`` is the newest page. An integer returns events
    with a greater id. The page is still only rows inside the 24-hour TTL.
    """
    registrations = bars.list_live_registrations(user_id=user_id)
    positions = bars.list_paper_positions(user_id=user_id)
    events, has_more = bars.paper_event_page(
        user_id=user_id, since_id=events_since, limit=events_limit
    )
    if events:
        cursor = events[-1][0]
    elif events_since is None:
        cursor = 0
    else:
        cursor = events_since
    running = {(strategy_id, version) for strategy_id, version, _doc, _size, _at in registrations}
    documents = _documents(registrations, positions, strategies, user_id)
    marks = _marks(bars, user_id, positions, documents)
    return {
        "mode": "paper",
        "simulated": True,
        "strategies": [_strategy(row, documents) for row in registrations],
        "positions": [
            _position(row, running, documents, marks) for row in positions
        ],
        "events": [
            {
                "id": event_id,
                "strategy_id": strategy_id,
                "version": version,
                "open_time": open_time,
                "kind": kind,
                "detail": detail,
            }
            for event_id, strategy_id, version, open_time, kind, detail in events
        ],
        "events_cursor": cursor,
        "events_has_more": has_more,
    }


def _documents(
    registrations: list[tuple[str, int, str, str, int]],
    positions: list[tuple[object, ...]],
    strategies: StrategyStore,
    user_id: str,
) -> dict[tuple[str, int], dict[str, Any] | None]:
    found: dict[tuple[str, int], dict[str, Any] | None] = {}
    for strategy_id, version, document_json, _bar_size, _registered_at in registrations:
        found[(strategy_id, version)] = _load_json(document_json)
    needed = {(str(row[0]), int(row[1])) for row in positions}
    for key in needed:
        if key in found:
            continue
        strategy_id, version = key
        try:
            record = strategies.get_version(user_id, strategy_id, version)
        except (StrategyNotFound, VersionNotFound):
            found[key] = None
            continue
        found[key] = record.ir if isinstance(record.ir, dict) else None
    return found


def _load_json(text: str) -> dict[str, Any] | None:
    try:
        loaded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _strategy(
    row: tuple[str, int, str, str, int],
    documents: dict[tuple[str, int], dict[str, Any] | None],
) -> dict[str, Any]:
    strategy_id, version, _document_json, bar_size, registered_at = row
    document = documents.get((strategy_id, version))
    return {
        "strategy_id": strategy_id,
        "version": version,
        "name": _name(document),
        "bar_size": bar_size,
        "registered_at": registered_at,
        "status": "running",
    }


def _position(
    row: tuple[object, ...],
    running: set[tuple[str, int]],
    documents: dict[tuple[str, int], dict[str, Any] | None],
    marks: dict[tuple[str, int, str], dict[str, Any] | None],
) -> dict[str, Any]:
    strategy_id = str(row[0])
    version = int(row[1])
    asset_id = str(row[2])
    qty = row[3]
    key = (strategy_id, version)
    if qty is None:
        state = "flat"
    elif key in running:
        state = "open"
    else:
        state = "open_unmanaged"
    return {
        "strategy_id": strategy_id,
        "version": version,
        "asset_id": asset_id,
        "state": state,
        "qty": qty,
        "open_cost": row[4],
        "entry": row[5],
        "basis": row[6],
        "realized": row[7],
        "trade_realized": row[8],
        "take_profit": row[9],
        "stop_loss": row[10],
        "anchor": row[11],
        "attached_at": row[12],
        "mark": marks.get((strategy_id, version, asset_id)),
    }


def _marks(
    bars: SqliteBarStore,
    user_id: str,
    positions: list[tuple[object, ...]],
    documents: dict[tuple[str, int], dict[str, Any] | None],
) -> dict[tuple[str, int, str], dict[str, Any] | None]:
    cached: dict[tuple[str, str], dict[str, Any] | None] = {}
    found: dict[tuple[str, int, str], dict[str, Any] | None] = {}
    for row in positions:
        strategy_id = str(row[0])
        version = int(row[1])
        asset_id = str(row[2])
        if row[3] is None:
            found[(strategy_id, version, asset_id)] = None
            continue
        document = documents.get((strategy_id, version))
        timeframe = _bar_size(document)
        mint = _mint(document, asset_id)
        if timeframe is None or mint is None:
            found[(strategy_id, version, asset_id)] = None
            continue
        series = (mint, timeframe)
        if series not in cached:
            cached[series] = _mark(bars, user_id, mint, timeframe)
        found[(strategy_id, version, asset_id)] = cached[series]
    return found


def _mark(
    bars: SqliteBarStore, user_id: str, mint: str, timeframe: str
) -> dict[str, Any] | None:
    latest = bars.latest_real_close(user_id=user_id, mint=mint, timeframe=timeframe)
    if latest is None:
        return None
    open_time, close = latest
    price = _close_text(close)
    if price is None:
        return None
    return {
        "label": "mark",
        "price": price,
        "open_time": open_time,
        "timeframe": timeframe,
        "source": "last_stored_close",
    }


def _close_text(value: float) -> str | None:
    if not math.isfinite(value):
        return None
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _bar_size(document: dict[str, Any] | None) -> str | None:
    if document is None:
        return None
    engine = document.get("engine")
    if not isinstance(engine, dict):
        return None
    size = engine.get("bar_size")
    if not isinstance(size, str) or size not in TIMEFRAME_SECONDS:
        return None
    return size


def _mint(document: dict[str, Any] | None, asset_id: str) -> str | None:
    if document is None:
        return None
    assets = document.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("id") != asset_id:
            continue
        symbol = asset.get("symbol")
        if not isinstance(symbol, str):
            return None
        known = BY_SYMBOL.get(symbol)
        if known is None:
            return None
        return known.mint
    return None


def _name(document: dict[str, Any] | None) -> str | None:
    if document is None:
        return None
    strategy = document.get("strategy")
    if not isinstance(strategy, dict):
        return None
    name = strategy.get("name")
    if not isinstance(name, str) or name == "":
        return None
    return name
