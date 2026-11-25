"""Manual-confirm signer. Real routes, a mock chain, and no key."""

from __future__ import annotations

import base64
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ohlcv.registry import BONK_MINT, JTO_MINT, JUP_MINT, SOL_MINT
from app.ohlcv.store import SqliteBarStore
from app.risk.fees import reset_day_ledger, set_day_clock, take_day
from app.risk.gate import LiveGate
from app.risk.guard import guard_unsigned
from app.risk.killfile import reset_process_halt
from app.risk.pending import pending_book
from app.risk.programs import JUPITER_V6_PROGRAM
from app.risk.route_ix import (
    _SWAP,
    decode_jupiter_route,
    encode_route_v1,
    encode_route_v2,
    minimum_out,
)
from app.risk.rpc import HttpChainRpc, MockChainRpc, RpcError, Simulation, TokenDelta
from app.risk.signer import USDC_MINT, LiveContext, reset_guarded
from app.venue.errors import VenueExecuteDisabled, VenueRejected
from app.venue.mock import MockVenue
from app.venue.tx import assemble_unsigned, split_transaction
from app.venue.types import AccountRole, DecodedInstruction, VenueExpiry, VenueQuote

_WALLET = "29d2S7vB453rNYFdR5Ycwt7y9haRT5fwVwL9zTmBhfV2"
_OTHER = "6k78AbasGMFFrhG95Pj6jQbqkVt7FQMhVgemxJovWKR6"
_USDC_ATA = "4Ss5JMkXAD9Z7cktFEdrqeMuT6jGMF1pVozTyPHZ6zT4"
_SOL_ATA = "5bV6jUfhDHCQVA1WfKBUnXUsboJgoKgkzkKcxr3joew5"
_RENT = "3JF3sEqM796hk5WFqA6EtmEwJQ9quALszsfJyvXNQKy3"
_DAY = "2026-11-25"
_NOW = 1_700_000_000
_IN = 2_500_000
_OUT = 25_000_000
_MIN = 24_875_000


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_day_ledger()
    reset_process_halt()
    pending_book().reset()
    reset_guarded()
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_KILL_FILE", raising=False)
    monkeypatch.delenv("LIVE_KILL", raising=False)
    monkeypatch.delenv("LIVE_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("LIVE_WALLET_PUBKEY", raising=False)
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)


def test_route_v2_and_route_v1_decode_min_out_and_slippage() -> None:
    assert len(_SWAP) == 152
    assert _SWAP[47] == "whirl_v2"
    assert _SWAP[17] == "bool"
    v2 = decode_jupiter_route(encode_route_v2(in_amount=_IN, quoted_out=_OUT, slippage_bps=50))
    assert v2 is not None and not isinstance(v2, str)
    assert v2.slippage_bps == 50
    assert v2.min_out == _MIN
    assert v2.min_out == minimum_out(_OUT, 50)
    v1 = decode_jupiter_route(encode_route_v1(in_amount=1_000, quoted_out=2_000, slippage_bps=50))
    assert v1 is not None and not isinstance(v1, str)
    assert v1.min_out == 1_990
    assert decode_jupiter_route(bytes.fromhex("d033ef977b2bed5c") + b"\x00" * 40) == "exact_out"
    assert decode_jupiter_route(b"\x01" * 8) is None
    zero = decode_jupiter_route(encode_route_v2(in_amount=100, quoted_out=100, slippage_bps=10_000))
    assert zero is not None and not isinstance(zero, str)
    assert zero.min_out == 0


def test_server_derives_notional_fees_authorities_sell_route_and_positions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirements 1 and 6. The body cannot supply these numbers."""
    client, rpc, store = _open(tmp_path, monkeypatch)
    lied = {
        **_intent(),
        "notional_usd": "0.01",
        "network_fee_lamports": 1,
        "freeze_authority_set": False,
    }
    rejected = client.post("/live/prepare", json=lied, headers=_headers())
    assert rejected.status_code == 422
    assert take_day(_DAY, store).notional_usd == Decimal("0")
    prepared = client.post("/live/prepare", json=_intent(), headers=_headers())
    assert prepared.status_code == 200
    body = prepared.json()
    trade = body["trade"]
    assert trade["input_mint"] == USDC_MINT
    assert trade["output_mint"] == SOL_MINT
    assert trade["in_amount"] == _IN
    assert trade["out_amount"] == _OUT
    assert trade["minimum_out_amount"] == _MIN
    assert trade["slippage_bps"] == 50
    assert trade["network_fee_lamports"] == 5_000
    assert trade["priority_fee_lamports"] == 1_000
    assert trade["rent_lamports"] == 1_000_000
    assert trade["notional_usd"] == "2.5"
    assert body["sent"] is False
    assert "send" not in rpc.calls
    frozen = _open(tmp_path, monkeypatch, freeze=1, name="freeze")
    assert frozen[0].post("/live/prepare", json=_intent(), headers=_headers()).json()["code"] == (
        "freeze_authority"
    )
    unsellable = _open(tmp_path, monkeypatch, sell_error=True, name="sell")
    unsold = unsellable[0].post("/live/prepare", json=_intent(), headers=_headers())
    assert unsold.json()["code"] == "sell_route"
    crowded = _open(
        tmp_path,
        monkeypatch,
        holdings=(
            ("a", JUP_MINT, 1_000_000),
            ("b", JTO_MINT, 1_000_000_000),
            ("c", BONK_MINT, 100_000),
        ),
        name="positions",
    )
    assert crowded[0].post("/live/prepare", json=_intent(), headers=_headers()).json()["code"] == (
        "position_cap"
    )
    rich = _open(tmp_path, monkeypatch, wallet_lamports=1_000_000_000_000, name="ceiling")
    assert rich[0].post("/live/prepare", json=_intent(), headers=_headers()).json()["code"] == (
        "wallet_ceiling"
    )
    store.close()


def test_output_mint_is_the_simulated_mint_and_must_be_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, rpc, store = _open(tmp_path, monkeypatch, output_mint=_OTHER)
    response = client.post("/live/prepare", json=_intent(), headers=_headers())
    assert response.status_code == 422
    assert response.json()["code"] == "allowlist"
    assert response.json()["sent"] is False
    assert "send" not in rpc.calls
    store.close()


def test_the_wallet_is_the_configured_pubkey_not_the_fee_payer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, rpc, store = _open(tmp_path, monkeypatch, payer=_OTHER)
    response = client.post("/live/prepare", json=_intent(), headers=_headers())
    assert response.status_code == 422
    assert response.json()["code"] == "fee_payer"
    assert "send" not in rpc.calls
    text = Path("app/risk/routes.py").read_text()
    assert "wallet=view" not in text
    assert "mark_ok=True" not in text
    assert "dead_man_halted=False" not in text
    store.close()


def test_dead_man_and_mark_come_from_server_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kill"
    path.write_bytes(b"0")
    monkeypatch.setenv("LIVE_KILL_FILE", str(path))
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_CONTROL_TOKEN", "test")
    monkeypatch.setenv("LIVE_WALLET_PUBKEY", _WALLET)
    _freeze()
    store = SqliteBarStore(tmp_path / "day.sqlite")
    app = create_app()
    app.state.live_day_store = store
    gate = LiveGate(kill_path=path)
    gate.dead_man = True
    assert path.read_bytes() == b"0"
    app.state.live_gate = gate
    app.state.live_context = _context(_rpc(), _desk())
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    halted = client.post("/live/prepare", json=_intent(), headers=_headers())
    assert halted.status_code == 409
    assert halted.json()["code"] == "dead_man"
    assert halted.json()["sent"] is False
    clear = LiveGate(kill_path=path)
    clear.note(now=1_000, interval=60)
    app.state.live_gate = clear
    marks = {USDC_MINT: Decimal("1")}
    app.state.live_context = _context(_rpc(), _desk(), marks=marks)
    missing = client.post("/live/prepare", json=_intent(), headers=_headers())
    assert missing.status_code == 422
    assert missing.json()["code"] == "missing_mark"
    store.close()


def test_a_fill_books_the_lamport_drop_and_realized_loss_halts_at_five_dollars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, rpc, store = _open(tmp_path, monkeypatch)
    prepared = client.post("/live/prepare", json=_intent(), headers=_headers()).json()
    signed = _sign(prepared["transaction_b64"])
    done = client.post(
        "/live/confirm",
        json={"pending_id": prepared["pending_id"], "signed_transaction_b64": signed},
        headers=_headers(),
    )
    assert done.status_code == 200
    assert done.json()["sent"] is True
    assert done.json()["confirmed"] is True
    assert done.json()["signature"] == "mock-signature"
    assert rpc.sent and rpc.sent[0] != b""
    ledger = take_day(_DAY, store)
    assert ledger.notional_usd == Decimal("2.5")
    assert ledger.network_lamports == 5_000
    assert ledger.priority_lamports == 1_000
    assert ledger.rent_lamports == 1_000_000
    assert ledger.fee_total_lamports() == 1_006_000
    assert ledger.realized_loss_usd == Decimal("0")
    again = client.post(
        "/live/confirm",
        json={"pending_id": prepared["pending_id"], "signed_transaction_b64": signed},
        headers=_headers(),
    )
    assert again.status_code == 422
    assert again.json()["code"] == "unknown_pending"
    assert rpc.calls.count("send") == 1
    store.close()
    loss_client, _loss_rpc, loss_store = _open(
        tmp_path,
        monkeypatch,
        name="loss",
        side="sell",
        in_amount=_OUT,
        out_amount=1,
        minimum=1,
        bar_price=Decimal("0.00004"),
        drop=5_000,
        rent=0,
    )
    for _ in range(3):
        sell = _intent(side="sell", action="exit", amount=_OUT)
        row = loss_client.post("/live/prepare", json=sell, headers=_headers())
        assert row.status_code == 200, row.json()
        payload = row.json()
        signed = _sign(payload["transaction_b64"])
        filled = loss_client.post(
            "/live/confirm",
            json={"pending_id": payload["pending_id"], "signed_transaction_b64": signed},
            headers=_headers(),
        )
        assert filled.status_code == 200
        assert filled.json()["confirmed"] is True
    booked = take_day(_DAY, loss_store)
    assert booked.realized_loss_usd >= Decimal("5")
    assert booked.halted is True
    assert booked.network_lamports == 15_000
    stopped = loss_client.post(
        "/live/prepare",
        json=_intent(side="sell", action="exit", amount=_OUT),
        headers=_headers(),
    )
    assert stopped.json()["code"] == "daily_halt"
    assert stopped.json()["sent"] is False
    loss_store.close()


def test_min_out_zero_and_wide_slippage_are_refused_before_sign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zero_client, zero_rpc, zero_store = _open(
        tmp_path,
        monkeypatch,
        name="zero",
        in_amount=100,
        instruction_slippage=10_000,
        quoted_out=100,
        out_amount=100,
        minimum=1,
    )
    zero = zero_client.post("/live/prepare", json=_intent(amount=100), headers=_headers())
    assert zero.status_code == 422
    assert zero.json()["code"] == "min_out"
    assert "send" not in zero_rpc.calls
    wide_client, wide_rpc, wide_store = _open(
        tmp_path,
        monkeypatch,
        name="wide",
        instruction_slippage=101,
        quoted_out=10,
        out_amount=10,
        minimum=10,
        in_amount=10,
        bar_price=Decimal("1"),
        marks={USDC_MINT: Decimal("1"), SOL_MINT: Decimal("1")},
    )
    wide = wide_client.post("/live/prepare", json=_intent(amount=10), headers=_headers())
    assert wide.status_code == 422
    assert wide.json()["code"] == "slippage"
    assert "send" not in wide_rpc.calls
    built = assemble_unsigned(
        payer=_WALLET,
        instructions=(
            DecodedInstruction(
                JUPITER_V6_PROGRAM,
                (AccountRole(USDC_MINT, False, False), AccountRole(SOL_MINT, False, False)),
                encode_route_v2(in_amount=100, quoted_out=100, slippage_bps=10_000),
            ),
        ),
        lookup_tables=(),
        recent_blockhash=b"\x11" * 32,
        quote=_bare_quote(100, 100, 1),
        last_valid_block_height=500,
    )
    direct = guard_unsigned(
        built,
        wallet=_WALLET,
        owned=frozenset({_WALLET}),
        rpc=MockChainRpc(height=10),
        quoted_in=100,
        quoted_out=100,
        quoted_minimum=1,
        input_account=_USDC_ATA,
        output_account=_SOL_ATA,
        input_mint=USDC_MINT,
        output_mint=SOL_MINT,
    )
    assert direct.code == "min_out"
    zero_store.close()
    wide_store.close()


def test_signed_bytes_must_match_and_the_kill_file_is_read_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kill"
    client, rpc, store = _open(tmp_path, monkeypatch, kill_path=path)
    prepared = client.post("/live/prepare", json=_intent(), headers=_headers()).json()
    raw = bytearray(base64.b64decode(prepared["transaction_b64"]))
    raw[-1] ^= 0x01
    flipped = base64.b64encode(bytes(raw)).decode("ascii")
    mismatch = client.post(
        "/live/confirm",
        json={"pending_id": prepared["pending_id"], "signed_transaction_b64": flipped},
        headers=_headers(),
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "byte_mismatch"
    assert rpc.sent == []
    prepared = client.post("/live/prepare", json=_intent(), headers=_headers()).json()
    monkeypatch.setenv("LIVE_WALLET_PUBKEY", _OTHER)
    moved = client.post(
        "/live/confirm",
        json={
            "pending_id": prepared["pending_id"],
            "signed_transaction_b64": _sign(prepared["transaction_b64"]),
        },
        headers=_headers(),
    )
    assert moved.status_code == 422
    assert moved.json()["code"] == "fee_payer"
    assert rpc.sent == []
    monkeypatch.setenv("LIVE_WALLET_PUBKEY", _WALLET)
    prepared = client.post("/live/prepare", json=_intent(), headers=_headers()).json()
    client.app.state.live_context = _aged(client.app.state.live_context, _NOW + 60)
    expired = client.post(
        "/live/confirm",
        json={
            "pending_id": prepared["pending_id"],
            "signed_transaction_b64": _sign(prepared["transaction_b64"]),
        },
        headers=_headers(),
    )
    assert expired.json()["code"] == "expired"
    assert rpc.sent == []
    prepared = client.post("/live/prepare", json=_intent(), headers=_headers()).json()
    rpc._height = 501
    stale = client.post(
        "/live/confirm",
        json={
            "pending_id": prepared["pending_id"],
            "signed_transaction_b64": _sign(prepared["transaction_b64"]),
        },
        headers=_headers(),
    )
    assert stale.json()["code"] == "stale_block"
    assert "block_height" in [item for item in rpc.calls if item == "block_height"]
    assert rpc.sent == []
    rpc._height = 10
    killing = _KillOnVerify(path, rpc)
    client.app.state.live_context = _swap_rpc(client.app.state.live_context, killing)
    prepared = client.post("/live/prepare", json=_intent(), headers=_headers()).json()
    blocked = client.post(
        "/live/confirm",
        json={
            "pending_id": prepared["pending_id"],
            "signed_transaction_b64": _sign(prepared["transaction_b64"]),
        },
        headers=_headers(),
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "killed"
    assert blocked.json()["sent"] is False
    assert "send" not in killing.calls
    assert path.read_bytes() == b"1"
    store.close()


def test_send_is_reachable_only_when_live_trading_is_exactly_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        import json

        method = json.loads(body)["method"]
        seen.append(method)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": "signed-id"},
        )

    rpc = HttpChainRpc("http://127.0.0.1", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RpcError, match="trading_disabled"):
        rpc.send(b"\x01")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    with pytest.raises(RpcError, match="trading_disabled"):
        rpc.send(b"\x01")
    assert seen == []
    with pytest.raises(RpcError, match="send_disabled"):
        rpc._call("sendTransaction", [])
    assert "sendTransaction" not in rpc.methods
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    assert rpc.send(b"\x02") == "signed-id"
    assert seen == ["sendTransaction"]


def test_the_signer_source_has_no_keypair() -> None:
    banned = ("Keypair", "from_seed", "nacl", "solders", "private_key", "secret_key", "mnemonic")
    roots = [Path("app/risk"), Path("app/venue")]
    for root in roots:
        for path in root.glob("*.py"):
            text = path.read_text()
            for word in banned:
                assert word not in text, f"{path} contains {word}"
    web = Path(__file__).resolve().parents[2] / "web"
    for folder in ("app", "components", "lib"):
        for path in (web / folder).rglob("*"):
            if path.suffix not in {".ts", ".tsx"}:
                continue
            text = path.read_text()
            for word in (*banned, "privateKey", "secretKey"):
                assert word not in text, f"{path} contains {word}"


class _KillOnVerify(MockChainRpc):
    def __init__(self, path: Path, source: MockChainRpc) -> None:
        super().__init__(
            height=source._height,
            simulation=source._simulation,
            accounts=source._accounts,
            holdings=source._holdings,
        )
        self._path = path

    def simulate(
        self,
        transaction: bytes,
        accounts: object,
        *,
        sig_verify: bool = False,
    ) -> Simulation:
        found = super().simulate(transaction, accounts, sig_verify=sig_verify)  # type: ignore[arg-type]
        if sig_verify:
            self._path.write_bytes(b"1")
        return found


class _Desk:
    def __init__(
        self,
        buy: MockVenue,
        *,
        sell_error: bool,
        payer: str | None,
        mode: str,
    ) -> None:
        self.buy = buy
        self.sell_error = sell_error
        self.payer = payer
        self.mode = mode
        self.executed = False

    def quote(self, request: object, *, now: int) -> VenueQuote:
        side = getattr(request, "side", "")
        output_mint = getattr(request, "output_mint", "")
        if self.mode == "buy" and side == "sell" and output_mint == USDC_MINT:
            if self.sell_error:
                raise VenueRejected("mint_mismatch")
            return _bare_quote(
                getattr(request, "amount"),
                getattr(request, "amount"),
                1,
                input_mint=getattr(request, "input_mint"),
                output_mint=USDC_MINT,
                impact="-0.1",
            )
        return self.buy.quote(request, now=now)  # type: ignore[arg-type]

    def build(
        self,
        quote: VenueQuote,
        *,
        taker: str,
        now: int,
        block_height: int | None = None,
        slippage_bps: int | None = None,
    ) -> object:
        return self.buy.build(
            quote,
            taker=self.payer or taker,
            now=now,
            block_height=block_height,
            slippage_bps=slippage_bps,
        )

    def execute(self, transaction: object = None) -> None:
        self.executed = True
        raise VenueExecuteDisabled("execute_disabled")


def _open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str = "day",
    freeze: int = 0,
    sell_error: bool = False,
    holdings: tuple[tuple[str, str, int], ...] = (),
    wallet_lamports: int = 1_000_000,
    payer: str | None = None,
    output_mint: str = SOL_MINT,
    side: str = "buy",
    in_amount: int = _IN,
    out_amount: int = _OUT,
    minimum: int = _MIN,
    bar_price: Decimal = Decimal("100"),
    drop: int = 1_006_000,
    rent: int = 1_000_000,
    instruction_slippage: int = 50,
    quoted_out: int | None = None,
    marks: dict[str, Decimal] | None = None,
    kill_path: Path | None = None,
) -> tuple[TestClient, MockChainRpc, SqliteBarStore]:
    path = kill_path if kill_path is not None else tmp_path / f"{name}-kill"
    path.write_bytes(b"0")
    monkeypatch.setenv("LIVE_KILL_FILE", str(path))
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_CONTROL_TOKEN", "test")
    monkeypatch.setenv("LIVE_WALLET_PUBKEY", _WALLET)
    _freeze()
    reset_guarded()
    pending_book().reset()
    reset_day_ledger()
    store = SqliteBarStore(tmp_path / f"{name}.sqlite")
    rpc = _rpc(
        output_mint=output_mint,
        in_amount=in_amount,
        out_amount=out_amount,
        wallet_lamports=wallet_lamports,
        holdings=holdings,
        freeze=freeze,
        drop=drop,
        rent=rent,
        side=side,
    )
    quoted = out_amount if quoted_out is None else quoted_out
    desk = _desk(
        in_amount=in_amount,
        out_amount=out_amount,
        minimum=minimum,
        quoted_out=quoted,
        slippage=instruction_slippage,
        sell_error=sell_error,
        payer=payer,
        side=side,
    )
    app = create_app()
    app.state.live_day_store = store
    app.state.live_gate = LiveGate(kill_path=path)
    app.state.live_context = _context(rpc, desk, bar_price=bar_price, marks=marks)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    return client, rpc, store


def _context(
    rpc: MockChainRpc,
    desk: _Desk,
    *,
    bar_price: Decimal = Decimal("100"),
    marks: dict[str, Decimal] | None = None,
) -> LiveContext:
    clock = {"now": _NOW}
    chosen = {
        USDC_MINT: Decimal("1"),
        SOL_MINT: Decimal("100"),
        JUP_MINT: Decimal("1"),
        JTO_MINT: Decimal("1"),
        BONK_MINT: Decimal("1"),
    }
    if marks is not None:
        chosen = marks
    return LiveContext(
        venue=desk,
        rpc=rpc,
        marks=chosen,
        pools={SOL_MINT: (Decimal("100000"), 200_000)},
        bar_age_bars=0,
        bar_price=bar_price,
        bar_seconds=60,
        now=lambda: clock["now"],
    )


def _aged(context: LiveContext, now: int) -> LiveContext:
    return LiveContext(
        venue=context.venue,
        rpc=context.rpc,
        marks=context.marks,
        pools=context.pools,
        bar_age_bars=context.bar_age_bars,
        bar_price=context.bar_price,
        bar_seconds=context.bar_seconds,
        now=lambda: now,
    )


def _swap_rpc(context: LiveContext, rpc: MockChainRpc) -> LiveContext:
    return LiveContext(
        venue=context.venue,
        rpc=rpc,
        marks=context.marks,
        pools=context.pools,
        bar_age_bars=context.bar_age_bars,
        bar_price=context.bar_price,
        bar_seconds=context.bar_seconds,
        now=context.now,
    )


def _rpc(
    *,
    output_mint: str = SOL_MINT,
    in_amount: int = _IN,
    out_amount: int = _OUT,
    wallet_lamports: int = 1_000_000,
    holdings: tuple[tuple[str, str, int], ...] = (),
    freeze: int = 0,
    drop: int = 1_006_000,
    rent: int = 1_000_000,
    side: str = "buy",
) -> MockChainRpc:
    if side == "buy":
        spent_mint, received_mint = USDC_MINT, output_mint
        spent_account, received_account = _USDC_ATA, _SOL_ATA
    else:
        spent_mint, received_mint = SOL_MINT, USDC_MINT
        spent_account, received_account = _SOL_ATA, _USDC_ATA
    token_before = in_amount + out_amount + 1_000
    lamport_before_wallet = max(5_000_000, drop + 1)
    deltas = (
        TokenDelta(spent_account, spent_mint, _WALLET, token_before, token_before - in_amount),
        TokenDelta(received_account, received_mint, _WALLET, 0, out_amount),
    )
    lamport_before = [(_WALLET, lamport_before_wallet)]
    lamport_after = [(_WALLET, lamport_before_wallet - drop)]
    if rent:
        lamport_before.append((_RENT, 0))
        lamport_after.append((_RENT, rent))
    mint = bytearray(82)
    mint[46:50] = freeze.to_bytes(4, "little")
    return MockChainRpc(
        height=10,
        simulation=Simulation(
            None,
            deltas,
            (),
            (),
            tuple(lamport_before),
            tuple(lamport_after),
        ),
        accounts={
            _WALLET: (wallet_lamports, b""),
            SOL_MINT: (1, bytes(mint)),
            output_mint: (1, bytes(mint)),
        },
        holdings=holdings,
    )


def _desk(
    *,
    in_amount: int = _IN,
    out_amount: int = _OUT,
    minimum: int = _MIN,
    quoted_out: int | None = None,
    slippage: int = 50,
    sell_error: bool = False,
    payer: str | None = None,
    side: str = "buy",
) -> _Desk:
    if side == "buy":
        input_mint, output_mint = USDC_MINT, SOL_MINT
    else:
        input_mint, output_mint = SOL_MINT, USDC_MINT
    shown = out_amount if quoted_out is None else quoted_out
    buy = MockVenue(
        input_mint=input_mint,
        output_mint=output_mint,
        in_amount=in_amount,
        out_amount=out_amount,
        minimum_out_amount=minimum,
        price_impact="-0.1",
        route_label="Jupiter",
        router="jupiter",
        last_valid_block_height=500,
        instructions=(
            DecodedInstruction(
                JUPITER_V6_PROGRAM,
                (AccountRole(SOL_MINT, False, False), AccountRole(USDC_MINT, False, False)),
                encode_route_v2(in_amount=in_amount, quoted_out=shown, slippage_bps=slippage),
            ),
        ),
        lookup_tables=(),
        recent_blockhash=b"\x11" * 32,
    )
    return _Desk(buy, sell_error=sell_error, payer=payer, mode=side)


def _bare_quote(
    in_amount: int,
    out_amount: int,
    minimum: int,
    *,
    input_mint: str = USDC_MINT,
    output_mint: str = SOL_MINT,
    impact: str = "-0.1",
) -> VenueQuote:
    return VenueQuote(
        input_mint,
        output_mint,
        in_amount,
        out_amount,
        minimum,
        impact,
        "Jupiter",
        "jupiter",
        VenueExpiry(500, None),
        "request",
        _NOW,
        "buy",
        50,
        None,
        None,
        None,
        "ExactIn",
    )


def _intent(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "side": "buy",
        "action": "entry",
        "mint": SOL_MINT,
        "amount": _IN,
    }
    body.update(overrides)
    return body


def _headers() -> dict[str, str]:
    return {"origin": "http://127.0.0.1", "x-live-token": "test"}


def _sign(encoded: str) -> str:
    raw = bytearray(base64.b64decode(encoded))
    _sigs, _message, count = split_transaction(bytes(raw))
    assert count == 1
    raw[1:65] = b"\x11" * 64
    return base64.b64encode(bytes(raw)).decode("ascii")


def _freeze() -> None:
    from datetime import UTC, datetime

    stamp = int(datetime(2026, 11, 25, 15, tzinfo=UTC).timestamp())
    set_day_clock(lambda: stamp)
