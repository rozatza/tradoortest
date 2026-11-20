"""Pick the DEX pool GeckoTerminal OHLCV will read.

Candidates are the pools on the pages we fetched (the public token-pools
endpoint, one page of up to 20 by default). GeckoTerminal ranks that page
by a blend of liquidity and 24h volume. We do not trust that order.

Tie-break, in order, among eligible DEX pools:

1. Highest ``reserve_in_usd``, parsed as a base-10 decimal. A missing,
   non-numeric, or negative reserve is not eligible.
2. If liquidity is equal, the earlier ``pool_created_at`` wins. A missing
   timestamp loses to any parsed timestamp.
3. If those still tie, the lexicographically smallest pool address wins.

A pump.fun bonding-curve pool is not eligible. That is a dex id of
``pump-fun``, ``pumpfun``, or ``pump.fun``, or a pool whose
``launchpad_details.completed`` is false. When every candidate is one of
those, the result is ``no DEX pool yet`` rather than a pool choice. An
empty candidate list with no bonding-curve pool is ``no pool``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ohlcv.errors import NoDexPoolYet, NoPool

# pump.fun's pre-migration venue. PumpSwap (``pumpswap``) is a DEX pool
# after migration and stays eligible.
BONDING_CURVE_DEX_IDS = frozenset({"pump-fun", "pumpfun", "pump.fun"})
POOL_PAGE_SIZE = 20
_MISSING_CREATED = 10**18


@dataclass(frozen=True, slots=True)
class ChosenPool:
    """The pool selected for one mint, at one resolution time."""

    address: str
    dex: str
    dex_id: str
    quote_token: str
    quote_symbol: str | None
    liquidity_usd: str
    resolved_at: int
    pool_created_at: int | None

    def public(self) -> dict[str, object]:
        """Fields the ingest result, the CLI, and the job result share."""
        return {
            "address": self.address,
            "dex": self.dex,
            "dex_id": self.dex_id,
            "quote_token": self.quote_token,
            "quote_symbol": self.quote_symbol,
            "liquidity_usd": self.liquidity_usd,
            "resolved_at": _iso(self.resolved_at),
        }


def choose_pool(payload: object, mint: str, *, resolved_at: int) -> ChosenPool:
    """Return the winner, or raise ``NoPool`` / ``NoDexPoolYet``."""
    eligible, bonding = _candidates(payload)
    if eligible:
        winner = min(eligible, key=_rank)
        return ChosenPool(
            address=winner.address,
            dex=winner.dex,
            dex_id=winner.dex_id,
            quote_token=winner.quote_token,
            quote_symbol=winner.quote_symbol,
            liquidity_usd=winner.liquidity_text,
            resolved_at=resolved_at,
            pool_created_at=winner.created_at,
        )
    if bonding:
        raise NoDexPoolYet("no DEX pool yet")
    raise NoPool(f"no pool for mint {mint}")


def token_is_ungraduated(payload: object) -> bool:
    """True when the token payload says the launchpad has not completed."""
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict):
        return False
    return _ungraduated(attributes.get("launchpad_details"), dex_id="")


@dataclass
class _Candidate:
    address: str
    dex: str
    dex_id: str
    quote_token: str
    quote_symbol: str | None
    liquidity: Decimal
    liquidity_text: str
    created_at: int | None


def _candidates(payload: object) -> tuple[list[_Candidate], bool]:
    if not isinstance(payload, dict):
        return [], False
    rows = payload.get("data")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return [], False
    included = _included(payload.get("included"))
    eligible: list[_Candidate] = []
    bonding = False
    for row in rows:
        parsed, is_bonding = _one(row, included)
        bonding = bonding or is_bonding
        if parsed is not None:
            eligible.append(parsed)
    return eligible, bonding


def _included(raw: object) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return found
    for row in raw:
        if not isinstance(row, dict):
            continue
        ident = row.get("id")
        if isinstance(ident, str) and ident != "":
            found[ident] = row
    return found


def _one(
    row: object, included: dict[str, dict[str, Any]]
) -> tuple[_Candidate | None, bool]:
    if not isinstance(row, dict):
        return None, False
    attributes = row.get("attributes")
    if not isinstance(attributes, dict):
        return None, False
    address = attributes.get("address")
    if not isinstance(address, str) or address.strip() == "":
        return None, False
    dex_id, dex_name = _dex(row, included)
    if _ungraduated(attributes.get("launchpad_details"), dex_id=dex_id):
        return None, True
    liquidity_text, liquidity = _liquidity(attributes.get("reserve_in_usd"))
    if liquidity is None or liquidity_text is None:
        return None, False
    quote_token, quote_symbol = _quote(row, included)
    return (
        _Candidate(
            address=address.strip(),
            dex=dex_name or dex_id or "unknown",
            dex_id=dex_id or "unknown",
            quote_token=quote_token,
            quote_symbol=quote_symbol,
            liquidity=liquidity,
            liquidity_text=liquidity_text,
            created_at=_time(attributes.get("pool_created_at")),
        ),
        False,
    )


def _ungraduated(details: object, *, dex_id: str) -> bool:
    if isinstance(details, dict) and details.get("completed") is False:
        return True
    if dex_id in BONDING_CURVE_DEX_IDS:
        completed = isinstance(details, dict) and details.get("completed") is True
        return not completed
    return False


def _dex(row: dict[str, Any], included: dict[str, dict[str, Any]]) -> tuple[str, str]:
    relationships = row.get("relationships")
    data = relationships.get("dex") if isinstance(relationships, dict) else None
    pointer = data.get("data") if isinstance(data, dict) else None
    ident = pointer.get("id") if isinstance(pointer, dict) else None
    dex_id = ident.strip() if isinstance(ident, str) else ""
    record = included.get(dex_id) if dex_id else None
    attributes = record.get("attributes") if isinstance(record, dict) else None
    name = attributes.get("name") if isinstance(attributes, dict) else None
    dex_name = name.strip() if isinstance(name, str) else ""
    return dex_id, dex_name


def _quote(
    row: dict[str, Any], included: dict[str, dict[str, Any]]
) -> tuple[str, str | None]:
    relationships = row.get("relationships")
    data = relationships.get("quote_token") if isinstance(relationships, dict) else None
    pointer = data.get("data") if isinstance(data, dict) else None
    ident = pointer.get("id") if isinstance(pointer, dict) else None
    if not isinstance(ident, str) or ident.strip() == "":
        return "unknown", None
    record = included.get(ident)
    attributes = record.get("attributes") if isinstance(record, dict) else None
    address = attributes.get("address") if isinstance(attributes, dict) else None
    symbol = attributes.get("symbol") if isinstance(attributes, dict) else None
    if isinstance(address, str) and address.strip() != "":
        quote = address.strip()
    elif ident.startswith("solana_"):
        quote = ident.split("_", 1)[1]
    else:
        quote = ident
    quote_symbol = symbol.strip() if isinstance(symbol, str) and symbol.strip() else None
    return quote, quote_symbol


def _liquidity(value: object) -> tuple[str | None, Decimal | None]:
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float)):
        text = format(value, "f") if isinstance(value, float) else str(value)
    else:
        return None, None
    if text == "":
        return None, None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None, None
    if not parsed.is_finite() or parsed < 0:
        return None, None
    return text, parsed


def _time(value: object) -> int | None:
    if not isinstance(value, str) or value.strip() == "":
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _rank(pool: _Candidate) -> tuple[Decimal, int, str]:
    created = pool.created_at if pool.created_at is not None else _MISSING_CREATED
    return (-pool.liquidity, created, pool.address)


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
