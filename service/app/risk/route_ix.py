"""Jupiter v6 route instruction decoder.

Exact-in routes store ``in_amount``, ``quoted_out_amount``, and
``slippage_bps``. Minimum-out is the integer the program uses:

``quoted_out_amount - quoted_out_amount * slippage_bps // 10_000``.

``route`` and ``shared_accounts_route`` put the route plan first.
``route_v2`` and ``shared_accounts_route_v2`` put the amounts first.
Exact-out and token-ledger routes are refused. An unknown swap variant
is refused. Nothing here signs.
"""

from __future__ import annotations

from dataclasses import dataclass

_ROUTE = bytes.fromhex("e517cb977ae3ad2a")
_ROUTE_V2 = bytes.fromhex("bb64facc31c4af14")
_SHARED = bytes.fromhex("c1209b3341d69c81")
_SHARED_V2 = bytes.fromhex("d19853937cfed8e9")
_EXACT_OUT = frozenset(
    {
        bytes.fromhex("d033ef977b2bed5c"),
        bytes.fromhex("9d8ab85215f4f324"),
        bytes.fromhex("b0d169a89a7d453e"),
        bytes.fromhex("3560e5cad8bbfa18"),
    }
)
_LEDGER = frozenset(
    {
        bytes.fromhex("96564774a75d0e68"),
        bytes.fromhex("e6798f50779f6aaa"),
    }
)

# Swap enum payload, in IDL order. ``whirl_v2`` is tag 47.
_SWAP = (
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "bool",
    "empty",
    "empty",
    "empty",
    "side",
    "empty",
    "empty",
    "side",
    "side",
    "bool",
    "bool",
    "empty",
    "empty",
    "bool",
    "empty",
    "bool",
    "side",
    "empty",
    "empty",
    "side",
    "side",
    "sym",
    "empty",
    "empty",
    "empty",
    "u32",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "side",
    "empty",
    "u32",
    "clone",
    "sanctum",
    "sanctum1",
    "sanctum1",
    "empty",
    "whirl_v2",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "bool",
    "empty",
    "bool",
    "bool",
    "empty",
    "empty",
    "side",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "pair",
    "empty",
    "empty",
    "empty",
    "remain",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "u64",
    "u64",
    "empty",
    "empty",
    "side",
    "goon",
    "humid",
    "empty",
    "side",
    "empty",
    "empty",
    "empty",
    "empty",
    "bool",
    "bool",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "empty",
    "defi",
    "bool",
    "empty",
    "bool",
    "side",
    "empty",
    "empty",
    "side",
    "reject",
    "empty",
    "empty",
    "empty",
    "empty",
    "side",
    "bool",
    "humid",
    "bool",
    "rfq",
    "bool",
    "u128",
    "vault",
    "empty",
    "side",
    "whale2",
    "bool",
    "empty",
    "bool",
    "empty",
    "empty",
    "hylo",
    "empty",
    "empty",
    "sanctum",
    "bool",
    "empty",
    "empty",
    "empty",
    "empty",
    "bool",
    "empty",
    "empty",
    "empty",
    "bool",
    "dyn2",
    "empty",
    "empty",
    "empty",
    "empty",
    "bool",
)


@dataclass(frozen=True, slots=True)
class RouteTerms:
    """Amounts the route instruction itself encodes."""

    kind: str
    in_amount: int
    quoted_out: int
    slippage_bps: int
    min_out: int


def minimum_out(quoted_out: int, slippage_bps: int) -> int | None:
    """The program's exact-in floor. ``None`` when the inputs cannot be one."""
    if isinstance(quoted_out, bool) or isinstance(slippage_bps, bool):
        return None
    if not isinstance(quoted_out, int) or not isinstance(slippage_bps, int):
        return None
    if quoted_out < 0 or slippage_bps < 0 or slippage_bps > 10_000:
        return None
    return quoted_out - quoted_out * slippage_bps // 10_000


def encode_route_v2(*, in_amount: int, quoted_out: int, slippage_bps: int) -> bytes:
    """One Saber step, used by tests and fixtures. Amounts sit in front."""
    step = bytes((0,)) + (10_000).to_bytes(2, "little") + bytes((0, 1))
    body = b"".join(
        (
            _ROUTE_V2,
            in_amount.to_bytes(8, "little"),
            quoted_out.to_bytes(8, "little"),
            slippage_bps.to_bytes(2, "little"),
            (0).to_bytes(2, "little"),
            (0).to_bytes(2, "little"),
            (1).to_bytes(4, "little"),
            step,
        )
    )
    return body


def encode_route_v1(*, in_amount: int, quoted_out: int, slippage_bps: int) -> bytes:
    """One Whirlpool step (tag 17, a-to-b). Amounts sit at the end."""
    step = bytes((17, 1, 100, 0, 1))
    return b"".join(
        (
            _ROUTE,
            (1).to_bytes(4, "little"),
            step,
            in_amount.to_bytes(8, "little"),
            quoted_out.to_bytes(8, "little"),
            slippage_bps.to_bytes(2, "little"),
            bytes((0,)),
        )
    )


def decode_jupiter_route(data: bytes) -> RouteTerms | str | None:
    """Terms, a refusal code, or ``None`` when ``data`` is not a route.

    Refusal codes are ``exact_out``, ``token_ledger``, and ``route_undecoded``.
    """
    if not isinstance(data, bytes | bytearray) or len(data) < 8:
        return None
    raw = bytes(data)
    disc = raw[:8]
    if disc in _EXACT_OUT:
        return "exact_out"
    if disc in _LEDGER:
        return "token_ledger"
    if disc == _ROUTE_V2:
        return _prefix(raw, "route_v2", fees=True)
    if disc == _SHARED_V2:
        return _prefix(raw, "route_v2")
    if disc == _ROUTE:
        return _suffix(raw, "route", fees=True)
    if disc == _SHARED:
        return _suffix(raw, "route")
    return None


def _prefix(data: bytes, kind: str, *, fees: bool = False) -> RouteTerms | str:
    if len(data) < 30:
        return "route_undecoded"
    in_amount = int.from_bytes(data[8:16], "little")
    quoted = int.from_bytes(data[16:24], "little")
    slippage = int.from_bytes(data[24:26], "little")
    if fees:
        platform = int.from_bytes(data[26:28], "little")
        positive = int.from_bytes(data[28:30], "little")
        if platform != 0 or positive != 0:
            return "platform_fee"
    pos = _plan(data, 30, version=2)
    if pos is None or pos != len(data):
        return "route_undecoded"
    return _terms(kind, in_amount, quoted, slippage)


def _suffix(data: bytes, kind: str, *, fees: bool = False) -> RouteTerms | str:
    pos = _plan(data, 8, version=1)
    if pos is None or pos + 19 != len(data):
        return "route_undecoded"
    in_amount = int.from_bytes(data[pos : pos + 8], "little")
    quoted = int.from_bytes(data[pos + 8 : pos + 16], "little")
    slippage = int.from_bytes(data[pos + 16 : pos + 18], "little")
    if fees and data[pos + 18] != 0:
        return "platform_fee"
    return _terms(kind, in_amount, quoted, slippage)


def _terms(kind: str, in_amount: int, quoted: int, slippage: int) -> RouteTerms | str:
    floor = minimum_out(quoted, slippage)
    if floor is None:
        return "route_undecoded"
    return RouteTerms(kind, in_amount, quoted, slippage, floor)


def _plan(data: bytes, pos: int, *, version: int) -> int | None:
    if pos + 4 > len(data):
        return None
    count = int.from_bytes(data[pos : pos + 4], "little")
    pos += 4
    if count < 1 or count > 16:
        return None
    for _ in range(count):
        pos = _swap(data, pos)
        if pos is None:
            return None
        width = 4 if version == 2 else 3
        if pos + width > len(data):
            return None
        if version == 1 and data[pos] > 100:
            return None
        pos += width
    return pos


def _swap(data: bytes, pos: int) -> int | None:
    if pos >= len(data):
        return None
    tag = data[pos]
    pos += 1
    if tag >= len(_SWAP):
        return None
    kind = _SWAP[tag]
    if kind == "reject":
        return None
    if kind == "empty":
        return pos
    if kind == "bool":
        return _bool(data, pos)
    if kind == "side":
        return _byte_max(data, pos, 1)
    if kind == "u32":
        return _fixed(data, pos, 4)
    if kind == "u64":
        return _fixed(data, pos, 8)
    if kind == "u128":
        return _fixed(data, pos, 16)
    if kind == "sym":
        return _fixed(data, pos, 16)
    if kind == "clone":
        nxt = _byte_max(data, pos, 255)
        if nxt is None:
            return None
        nxt = _bool(data, nxt)
        return None if nxt is None else _bool(data, nxt)
    if kind == "sanctum":
        return _fixed(data, pos, 10)
    if kind == "sanctum1":
        return _fixed(data, pos, 5)
    if kind == "pair":
        return _fixed(data, pos, 2)
    if kind == "goon":
        nxt = _bool(data, pos)
        return None if nxt is None else _fixed(data, nxt, 1)
    if kind == "humid":
        nxt = _fixed(data, pos, 8)
        return None if nxt is None else _bool(data, nxt)
    if kind == "whirl_v2" or kind == "defi":
        nxt = _bool(data, pos)
        return None if nxt is None else _option_remaining(data, nxt)
    if kind == "remain":
        return _remaining(data, pos)
    if kind == "vault":
        return _fixed(data, pos, 48)
    if kind == "whale2":
        nxt = _byte_max(data, pos, 1)
        return None if nxt is None else _fixed(data, nxt, 16)
    if kind == "hylo":
        return _byte_max(data, pos, 7)
    if kind == "rfq":
        return _rfq(data, pos)
    if kind == "dyn2":
        return _dyn2(data, pos)
    return None


def _bool(data: bytes, pos: int) -> int | None:
    return _byte_max(data, pos, 1)


def _byte_max(data: bytes, pos: int, limit: int) -> int | None:
    if pos >= len(data) or data[pos] > limit:
        return None
    return pos + 1


def _fixed(data: bytes, pos: int, width: int) -> int | None:
    end = pos + width
    if end > len(data):
        return None
    return end


def _option_remaining(data: bytes, pos: int) -> int | None:
    if pos >= len(data):
        return None
    tag = data[pos]
    pos += 1
    if tag == 0:
        return pos
    if tag != 1:
        return None
    return _remaining(data, pos)


def _remaining(data: bytes, pos: int) -> int | None:
    if pos + 4 > len(data):
        return None
    count = int.from_bytes(data[pos : pos + 4], "little")
    pos += 4
    if count > 64:
        return None
    return _fixed(data, pos, count * 2)


def _rfq(data: bytes, pos: int) -> int | None:
    """``side`` then ``fill_data`` as a byte vector.

    A fill-data layout other than ``Vec<u8>`` fails the exact-consume
    check and the route is refused.
    """
    pos = _byte_max(data, pos, 1)
    if pos is None or pos + 4 > len(data):
        return None
    count = int.from_bytes(data[pos : pos + 4], "little")
    if count > 1232:
        return None
    return _fixed(data, pos + 4, count)


def _candidate(data: bytes, pos: int) -> int | None:
    if pos >= len(data):
        return None
    tag = data[pos]
    pos += 1
    if tag in (0, 2):
        nxt = _fixed(data, pos, 8)
        return None if nxt is None else _bool(data, nxt)
    if tag in (1, 5, 7, 8, 9):
        return _bool(data, pos)
    if tag in (3, 4, 6):
        return pos
    if tag == 10:
        nxt = _bool(data, pos)
        return None if nxt is None else _option_remaining(data, nxt)
    return None


def _dyn2(data: bytes, pos: int) -> int | None:
    if pos + 4 > len(data):
        return None
    count = int.from_bytes(data[pos : pos + 4], "little")
    pos += 4
    if count > 16:
        return None
    for _ in range(count):
        pos = _candidate(data, pos)
        if pos is None:
            return None
        pos = _fixed(data, pos, 4)
        if pos is None:
            return None
    return _fixed(data, pos, 2)
