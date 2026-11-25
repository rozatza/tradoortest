"""Chain reads behind an interface. The mock is what tests use.

The HTTP client speaks JSON-RPC with ``httpx``. It requests block height,
account info, and ``simulateTransaction``. It does not call
``sendTransaction`` and it does not sign.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.risk.programs import KNOWN_PROGRAMS
from app.venue.address import encode
from app.venue.checks import require_block_height
from app.venue.errors import VenueError

_LOOKUP_META = 56
_ACTIVE_SLOT = (1 << 64) - 1


class RpcError(VenueError):
    """The RPC read failed. The message is a short code."""


@dataclass(frozen=True, slots=True)
class LookupTable:
    address: str
    active: bool
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TokenDelta:
    account: str
    mint: str
    owner: str
    before: int
    after: int


@dataclass(frozen=True, slots=True)
class AuthorityView:
    account: str
    mint_authority: bytes
    freeze_authority: bytes


@dataclass(frozen=True, slots=True)
class Simulation:
    err: str | None
    deltas: tuple[TokenDelta, ...]
    authorities_before: tuple[AuthorityView, ...]
    authorities_after: tuple[AuthorityView, ...]
    lamports_before: tuple[tuple[str, int], ...]
    lamports_after: tuple[tuple[str, int], ...]
    owner_changed: tuple[str, ...] = ()
    unparsed: tuple[str, ...] = ()
    controls_changed: tuple[str, ...] = ()
    closed: tuple[str, ...] = ()


class ChainRpc(Protocol):
    """Block height, lookup tables, and simulation. No submit method."""

    def block_height(self) -> int:
        """The chain's current block height. Not a quote field."""

    def lookup_table(self, address: str) -> LookupTable | None:
        """The on-chain table, or None when the account is missing."""

    def simulate(self, transaction: bytes, accounts: Sequence[str]) -> Simulation:
        """Pre-sign simulation. ``sigVerify`` stays false."""


def chain_block_height(rpc: ChainRpc) -> int:
    """Height from ``rpc`` only. Callers must not pass a quote field."""
    return require_block_height(rpc.block_height())


def classify_account(data: bytes) -> str:
    """``mint``, ``token``, ``empty``, or ``unparsed``.

    Classic mint accounts are 82 bytes and classic token accounts are
    165. Anything else, including a Token-2022 extension, is unparsed
    and the guard rejects it.
    """
    if data == b"":
        return "empty"
    if len(data) == 82:
        return "mint"
    if len(data) == 165:
        return "token"
    return "unparsed"


def parse_lookup_table(data: bytes) -> tuple[bool, tuple[str, ...]]:
    """Active flag and addresses from an address-lookup-table account."""
    if len(data) < _LOOKUP_META or (len(data) - _LOOKUP_META) % 32 != 0:
        raise RpcError("bad_lookup_table")
    deactivation = int.from_bytes(data[0:8], "little")
    active = deactivation == _ACTIVE_SLOT
    addresses: list[str] = []
    start = _LOOKUP_META
    while start < len(data):
        addresses.append(encode(data[start : start + 32]))
        start += 32
    return active, tuple(addresses)


class MockChainRpc:
    """Scripted chain. Tests never open a socket."""

    def __init__(
        self,
        *,
        height: int,
        tables: dict[str, LookupTable] | None = None,
        simulation: Simulation | None = None,
    ) -> None:
        self._height = height
        self._tables = {} if tables is None else tables
        self._simulation = simulation
        self.calls: list[object] = []

    def block_height(self) -> int:
        self.calls.append("block_height")
        return self._height

    def lookup_table(self, address: str) -> LookupTable | None:
        self.calls.append(("lookup", address))
        return self._tables.get(address)

    def simulate(self, transaction: bytes, accounts: Sequence[str]) -> Simulation:
        self.calls.append("simulate")
        del transaction, accounts
        if self._simulation is None:
            raise RpcError("no_simulation")
        return self._simulation


class HttpChainRpc:
    """JSON-RPC over the existing HTTP client. No send, no signature."""

    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self._url = url
        self._client = client
        self._owns = client is None
        self.methods: list[str] = []

    def close(self) -> None:
        if self._owns and self._client is not None:
            self._client.close()

    def block_height(self) -> int:
        payload = self._call("getBlockHeight", [{"commitment": "confirmed"}])
        if isinstance(payload, bool) or not isinstance(payload, int):
            raise RpcError("bad_block_height")
        return payload

    def lookup_table(self, address: str) -> LookupTable | None:
        info = self._account(address)
        if info is None:
            return None
        active, addresses = parse_lookup_table(info[1])
        return LookupTable(address, active, addresses)

    def simulate(self, transaction: bytes, accounts: Sequence[str]) -> Simulation:
        wanted = tuple(accounts)
        before = [self._account(item) for item in wanted]
        encoded = base64.b64encode(transaction).decode("ascii")
        body = self._call(
            "simulateTransaction",
            [
                encoded,
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": True,
                    "commitment": "confirmed",
                    "accounts": {"encoding": "base64", "addresses": list(wanted)},
                },
            ],
        )
        if not isinstance(body, dict):
            raise RpcError("bad_simulation")
        value = body.get("value")
        if not isinstance(value, dict):
            raise RpcError("bad_simulation")
        err = value.get("err")
        err_text = None if err is None else "simulation_failed"
        after_rows = value.get("accounts")
        if not isinstance(after_rows, list) or len(after_rows) != len(wanted):
            raise RpcError("bad_simulation")
        return _simulation_from(wanted, before, after_rows, err_text)

    def _account(self, address: str) -> tuple[int, bytes] | None:
        payload = self._call(
            "getAccountInfo",
            [address, {"encoding": "base64", "commitment": "confirmed"}],
        )
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise RpcError("bad_account")
        value = payload.get("value")
        if value is None:
            return None
        return _account_value(value)

    def _call(self, method: str, params: list[object]) -> object:
        if method == "sendTransaction":
            raise RpcError("send_disabled")
        self.methods.append(method)
        client = self._http()
        try:
            response = client.post(
                self._url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
        except httpx.HTTPError:
            raise RpcError("rpc_transport") from None
        if response.status_code != 200:
            raise RpcError("rpc_status")
        try:
            body = response.json()
        except ValueError:
            raise RpcError("rpc_status") from None
        if not isinstance(body, dict) or body.get("error") is not None:
            raise RpcError("rpc_status")
        if "result" not in body:
            raise RpcError("rpc_status")
        return body["result"]

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=5.0, follow_redirects=False)
            self._owns = True
        return self._client


def default_rpc() -> HttpChainRpc | None:
    """The configured URL, or None. Unset does not open a socket."""
    raw = os.environ.get("SOLANA_RPC_URL")
    if raw is None or raw.strip() == "":
        return None
    return HttpChainRpc(raw.strip())


def _account_value(value: object) -> tuple[int, bytes]:
    if not isinstance(value, dict):
        raise RpcError("bad_account")
    lamports = value.get("lamports")
    data = value.get("data")
    if isinstance(lamports, bool) or not isinstance(lamports, int):
        raise RpcError("bad_account")
    if not isinstance(data, list) or len(data) < 1 or not isinstance(data[0], str):
        raise RpcError("bad_account")
    try:
        raw = base64.b64decode(data[0], validate=True)
    except ValueError:
        raise RpcError("bad_account") from None
    return lamports, raw


def _simulation_from(
    accounts: tuple[str, ...],
    before: list[tuple[int, bytes] | None],
    after_rows: list[object],
    err: str | None,
) -> Simulation:
    deltas: list[TokenDelta] = []
    authorities_before: list[AuthorityView] = []
    authorities_after: list[AuthorityView] = []
    lamports_before: list[tuple[str, int]] = []
    lamports_after: list[tuple[str, int]] = []
    owner_changed: list[str] = []
    unparsed: list[str] = []
    controls_changed: list[str] = []
    closed: list[str] = []
    for address, prior, row in zip(accounts, before, after_rows, strict=True):
        if address in KNOWN_PROGRAMS:
            continue
        if prior is not None:
            lamports_before.append((address, prior[0]))
            _note_unparsed(address, prior[1], unparsed)
        if row is None:
            if prior is not None:
                closed.append(address)
            continue
        lamports, data = _account_value(row)
        lamports_after.append((address, lamports))
        _note_unparsed(address, data, unparsed)
        if prior is None or address in unparsed:
            continue
        prior_auth = _mint_authorities(prior[1])
        next_auth = _mint_authorities(data)
        if prior_auth is not None and next_auth is not None:
            authorities_before.append(AuthorityView(address, prior_auth[0], prior_auth[1]))
            authorities_after.append(AuthorityView(address, next_auth[0], next_auth[1]))
        prior_token = _token_amount(prior[1])
        next_token = _token_amount(data)
        if prior_token is None or next_token is None:
            continue
        if prior_token.owner != next_token.owner:
            owner_changed.append(address)
        if _controls_differ(prior_token, next_token):
            controls_changed.append(address)
        deltas.append(
            TokenDelta(
                address,
                prior_token.mint,
                prior_token.owner,
                prior_token.amount,
                next_token.amount,
            )
        )
    return Simulation(
        err,
        tuple(deltas),
        tuple(authorities_before),
        tuple(authorities_after),
        tuple(lamports_before),
        tuple(lamports_after),
        tuple(owner_changed),
        tuple(unparsed),
        tuple(controls_changed),
        tuple(closed),
    )


def _mint_authorities(data: bytes) -> tuple[bytes, bytes] | None:
    """Mint authority and freeze authority from a classic 82-byte mint."""
    if len(data) != 82:
        return None
    return data[0:36], data[46:82]


@dataclass(frozen=True, slots=True)
class TokenView:
    """Classic token account. Delegate and close authority are part of the check."""

    mint: str
    owner: str
    amount: int
    delegate: bytes
    delegated_amount: int
    close_authority: bytes


def _note_unparsed(address: str, data: bytes, found: list[str]) -> None:
    if address in found:
        return
    if classify_account(data) == "unparsed":
        found.append(address)
        return
    if len(data) == 165 and _token_amount(data) is None:
        found.append(address)


def _controls_differ(left: TokenView, right: TokenView) -> bool:
    if left.delegate != right.delegate or left.close_authority != right.close_authority:
        return True
    return left.delegated_amount != right.delegated_amount


def _option_key(data: bytes, tag_at: int) -> bytes | None:
    """A COption pubkey. ``b""`` when the tag is 0. None when the tag is neither."""
    tag = int.from_bytes(data[tag_at : tag_at + 4], "little")
    if tag == 0:
        return b""
    if tag == 1:
        return bytes(data[tag_at + 4 : tag_at + 36])
    return None


def _token_amount(data: bytes) -> TokenView | None:
    """Mint, owner, amount, delegate, delegated amount, and close authority.

    A 165-byte account with a delegate or close-authority tag other than
    0 or 1 is not parsed. The caller treats that as unverified.
    """
    if len(data) != 165:
        return None
    delegate = _option_key(data, 72)
    close_authority = _option_key(data, 129)
    if delegate is None or close_authority is None:
        return None
    return TokenView(
        encode(data[0:32]),
        encode(data[32:64]),
        int.from_bytes(data[64:72], "little"),
        delegate,
        int.from_bytes(data[121:129], "little"),
        close_authority,
    )
