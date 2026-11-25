"""Risk engine, kill switch, and pre-sign guard. No socket and no key."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import LOCAL_USER_ID
from app.live.scheduler import PaperScheduler
from app.main import create_app
from app.ohlcv.registry import BONK_MINT, JTO_MINT, JUP_MINT, SOL_MINT, WIF_MINT
from app.ohlcv.store import SqliteBarStore
from app.risk.access import host_allowed, origin_allowed
from app.risk.bind import require_loopback
from app.risk.control import apply_kill
from app.risk.engine import LiveOrder, evaluate, startup_refuses_wallet
from app.risk.fees import (
    DayLedger,
    FeeLines,
    empty_day,
    record_realized_loss,
    reset_day_ledger,
    roll_day,
    save_day,
    server_day,
    set_day_clock,
    take_day,
)
from app.risk.gate import LiveGate
from app.risk.guard import (
    address_of,
    advance_nonce_data,
    compute_limit_data,
    compute_price_data,
    guard_unsigned,
    system_transfer_data,
)
from app.risk.killfile import (
    kill_halted,
    process_halted,
    read_kill_state,
    reset_process_halt,
    write_kill_halt,
)
from app.risk.limits import (
    ALLOWLIST_MINTS,
    DAILY_FEE_LAMPORTS,
    DAILY_LOSS_USD,
    DAILY_NOTIONAL_USD,
    DEAD_MAN_TICKS,
    MAX_CU_LIMIT,
    MAX_CU_PRICE_MICRO_LAMPORTS,
    MAX_DIVERGENCE,
    MAX_FEE_LAMPORTS,
    MAX_OPEN_POSITIONS,
    MAX_PRICE_IMPACT,
    MAX_PRIORITY_FEE_LAMPORTS,
    MAX_SLIPPAGE_BPS,
    MIN_POOL_AGE_SECONDS,
    MIN_POOL_LIQUIDITY_USD,
    PER_TRADE_USD,
    SLIPPAGE_BPS,
    STALE_BARS,
    WALLET_CEILING_USD,
    order_slippage_bps,
)
from app.risk.order import quote_and_build
from app.risk.pending import pending_book
from app.risk.programs import (
    ADDRESS_LOOKUP_PROGRAM,
    ASSOCIATED_TOKEN_PROGRAM,
    COMPUTE_BUDGET_PROGRAM,
    JUPITER_V6_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_CLOSE,
    TOKEN_PROGRAM,
)
from app.risk.rpc import (
    AuthorityView,
    HttpChainRpc,
    LookupTable,
    MockChainRpc,
    Simulation,
    TokenDelta,
    classify_account,
    default_rpc,
    parse_lookup_table,
)
from app.venue.address import address_bytes
from app.venue.errors import VenueExecuteDisabled
from app.venue.flags import live_trading_enabled
from app.venue.jupiter import JupiterVenue
from app.venue.mock import MockVenue
from app.venue.tx import assemble_unsigned, decode_unsigned_v0
from app.venue.types import (
    AccountRole,
    DecodedInstruction,
    ResolvedLookupTable,
    UnsignedTransaction,
    VenueExpiry,
    VenueQuote,
)

_DAY = "2026-11-25"
_WALLET = "29d2S7vB453rNYFdR5Ycwt7y9haRT5fwVwL9zTmBhfV2"
_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_PROGRAM = "3JF3sEqM796hk5WFqA6EtmEwJQ9quALszsfJyvXNQKy3"
_POOL = "4Ss5JMkXAD9Z7cktFEdrqeMuT6jGMF1pVozTyPHZ6zT4"
_TABLE = "5bV6jUfhDHCQVA1WfKBUnXUsboJgoKgkzkKcxr3joew5"
_OTHER = "6k78AbasGMFFrhG95Pj6jQbqkVt7FQMhVgemxJovWKR6"
_LEGACY = "4bc6fcc2fce84df4dbb0844e10e4e86e82d30e4d895112d4a8482205d2264798"
_RUNNER = "d2989f02b4b5ff99aaa5833e922145e9638fa3898200615dd6141cc006a0c126"


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_day_ledger()
    reset_process_halt()
    pending_book().reset()
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_KILL_FILE", raising=False)
    monkeypatch.delenv("LIVE_KILL", raising=False)
    monkeypatch.delenv("LIVE_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)


def _order(**overrides: object) -> LiveOrder:
    base: dict[str, object] = {
        "side": "buy",
        "action": "entry",
        "mint": SOL_MINT,
        "notional_usd": Decimal("2.50"),
        "slippage_bps": 50,
        "price_impact": "-0.1",
        "pool_liquidity_usd": Decimal("100000"),
        "pool_age_seconds": 200_000,
        "freeze_authority_set": False,
        "mint_authority_set": False,
        "sell_route_ok": True,
        "sell_route_impact": "0.1",
        "bar_age_bars": 0,
        "bar_price": Decimal("100"),
        "quote_price": Decimal("100"),
        "open_positions": 0,
        "asset_already_open": False,
        "fees": FeeLines(5_000, 1_000, 0),
        "wallet_value_usd": Decimal("20"),
        "mark_ok": True,
        "trading_enabled": True,
        "kill_halted": False,
        "dead_man_halted": False,
        "out_amount": 10_000,
        "minimum_out_amount": 9_950,
    }
    base.update(overrides)
    return LiveOrder(**base)  # type: ignore[arg-type]


def _check(order: LiveOrder, ledger: DayLedger | None = None) -> object:
    book = empty_day(_DAY) if ledger is None else ledger
    return evaluate(order, book, today=_DAY)


def test_defaults_match_the_canary() -> None:
    assert PER_TRADE_USD == Decimal("2.50")
    assert DAILY_NOTIONAL_USD == Decimal("10")
    assert DAILY_LOSS_USD == Decimal("5")
    assert MAX_OPEN_POSITIONS == 3
    assert WALLET_CEILING_USD == Decimal("25")
    assert ALLOWLIST_MINTS == frozenset({SOL_MINT, JUP_MINT, JTO_MINT, BONK_MINT, WIF_MINT})
    assert SLIPPAGE_BPS == 50
    assert MAX_SLIPPAGE_BPS == 100
    assert order_slippage_bps() == 50
    assert MAX_PRICE_IMPACT == Decimal("1")
    assert MIN_POOL_LIQUIDITY_USD == Decimal("50000")
    assert MIN_POOL_AGE_SECONDS == 86_400
    assert STALE_BARS == 2
    assert MAX_DIVERGENCE == Decimal("0.01")
    assert DEAD_MAN_TICKS == 2
    assert MAX_FEE_LAMPORTS == 5_000_000
    assert DAILY_FEE_LAMPORTS == 20_000_000
    assert MAX_CU_LIMIT == 1_400_000
    assert MAX_CU_PRICE_MICRO_LAMPORTS == 1_000_000
    assert MAX_PRIORITY_FEE_LAMPORTS == 100_000


def test_live_trading_is_on_only_for_the_exact_string_1() -> None:
    assert live_trading_enabled("1") is True
    for raw in ("", "0", "true", "TRUE", "yes", "on", " 1", "1 ", None):
        assert live_trading_enabled(raw) is False


def test_a_passing_order_books_notional_and_fees_but_not_loss() -> None:
    result = _check(_order())
    assert result.accepted is True
    assert result.resized is False
    assert result.notional_usd == Decimal("2.50")
    assert result.ledger.notional_usd == Decimal("2.50")
    assert result.ledger.realized_loss_usd == Decimal("0")
    assert result.ledger.network_lamports == 5_000
    assert result.ledger.priority_lamports == 1_000
    assert result.ledger.halted is False
    lines = result.ledger.cost_lines()
    assert lines["strategy_realized_loss_usd"] == "0"
    assert lines["network_fee_lamports"] == 5_000


def test_each_gate_rejects_without_resizing() -> None:
    cases = {
        "trading_disabled": {"trading_enabled": False},
        "killed": {"kill_halted": True},
        "dead_man": {"dead_man_halted": True},
        "missing_mark": {"mark_ok": False},
        "per_trade": {"notional_usd": Decimal("2.51")},
        "allowlist": {"mint": _USDC},
        "slippage_param": {"slippage_bps": None},
        "price_impact": {"price_impact": "1.5"},
        "pool_liquidity": {"pool_liquidity_usd": None},
        "pool_age": {"pool_age_seconds": 10},
        "freeze_authority": {"freeze_authority_set": True},
        "mint_authority": {"mint_authority_set": None},
        "sell_route": {"sell_route_ok": False},
        "stale_bar": {"bar_age_bars": 3},
        "divergence": {"quote_price": Decimal("102")},
        "missing_fee": {"fees": None},
        "wallet_ceiling": {"wallet_value_usd": Decimal("25.01")},
    }
    for code, fields in cases.items():
        result = _check(_order(**fields))
        assert result.accepted is False
        assert result.code == code
        assert result.resized is False
        assert result.notional_usd == _order(**fields).notional_usd
        assert result.ledger.notional_usd == Decimal("0")


def test_exact_caps_pass_and_one_step_past_rejects() -> None:
    assert _check(_order(notional_usd=Decimal("2.50"))).accepted is True
    assert _check(_order(notional_usd=Decimal("2.51"))).code == "per_trade"
    full = _check(_order(), DayLedger(_DAY, notional_usd=Decimal("7.50")))
    assert full.accepted is True
    assert full.ledger.notional_usd == Decimal("10")
    over = _check(_order(), DayLedger(_DAY, notional_usd=Decimal("8")))
    assert over.code == "daily_notional"
    assert over.ledger.halted is True
    stuck = _check(_order(notional_usd=Decimal("1")), over.ledger)
    assert stuck.code == "daily_halt"
    assert _check(_order(bar_age_bars=2)).accepted is True
    assert _check(_order(quote_price=Decimal("101"))).accepted is True
    assert _check(_order(wallet_value_usd=Decimal("25"))).accepted is True
    assert _check(_order(slippage_bps=51)).code == "slippage_param"
    loose = _order(out_amount=10_000, minimum_out_amount=1)
    assert _check(loose).code == "slippage"
    assert _check(_order(price_impact="1")).accepted is True
    assert _check(_order(price_impact="-1")).accepted is True
    assert _check(_order(pool_liquidity_usd=Decimal("49999"))).code == "pool_liquidity"
    assert _check(_order(pool_age_seconds=86_400)).accepted is True
    assert _check(_order(pool_age_seconds=86_399)).code == "pool_age"
    assert _check(_order(sell_route_impact="5")).code == "sell_route"
    assert _check(_order(open_positions=3)).code == "position_cap"
    assert _check(_order(asset_already_open=True)).code == "asset_open"
    assert _check(_order(open_positions=2)).accepted is True
    assert _check(_order(side="short", action="entry")).code == "bad_side"
    assert _check(_order(side="sell", action="entry")).code == "bad_side"


def test_fees_are_not_strategy_loss_and_have_their_own_caps() -> None:
    booked = record_realized_loss(empty_day(_DAY), Decimal("-1"))
    assert booked.realized_loss_usd == Decimal("1")
    assert booked.network_lamports == 0
    assert booked.halted is False
    gain = record_realized_loss(booked, Decimal("10"))
    assert gain.realized_loss_usd == Decimal("1")
    halted = record_realized_loss(empty_day(_DAY), Decimal("-5"))
    assert halted.halted is True
    assert halted.network_lamports == 0
    loss = _check(_order(), DayLedger(_DAY, realized_loss_usd=Decimal("5")))
    assert loss.code == "daily_loss"
    assert loss.ledger.halted is True
    huge = _check(_order(fees=FeeLines(5_000_001, 0, 0)))
    assert huge.code == "per_tx_fee"
    assert huge.ledger.halted is False
    exact = _check(_order(fees=FeeLines(5_000_000, 0, 0)))
    assert exact.accepted is True
    day = DayLedger(_DAY, network_lamports=19_000_000)
    fee = _check(_order(fees=FeeLines(2_000_000, 0, 0)), day)
    assert fee.code == "daily_fee"
    assert fee.ledger.halted is True
    assert fee.ledger.realized_loss_usd == Decimal("0")


def test_a_clock_step_backward_does_not_reset_the_day() -> None:
    stored = DayLedger(
        "2026-11-26",
        notional_usd=Decimal("9"),
        realized_loss_usd=Decimal("4"),
        network_lamports=100,
        halted=True,
    )
    rolled = roll_day(stored, "2026-11-25")
    assert rolled is stored
    result = evaluate(_order(), stored, today="2026-11-25")
    assert result.code == "daily_halt"
    assert result.ledger.notional_usd == Decimal("9")
    assert result.ledger.realized_loss_usd == Decimal("4")


def test_startup_refuses_a_wallet_above_the_ceiling() -> None:
    assert startup_refuses_wallet(None) == "missing_mark"
    assert startup_refuses_wallet(Decimal("25.01")) == "above_ceiling"
    assert startup_refuses_wallet(Decimal("25")) is None
    assert startup_refuses_wallet(Decimal("0")) is None


def test_a_sell_still_passes_when_the_ceiling_is_already_over() -> None:
    order = _order(
        side="sell",
        action="exit",
        wallet_value_usd=Decimal("80"),
        open_positions=3,
        asset_already_open=True,
    )
    result = _check(order)
    assert result.accepted is True
    assert result.code == "ok"


def test_flatten_skips_halt_gates_and_still_checks_slippage() -> None:
    order = _order(
        side="sell",
        action="flatten",
        trading_enabled=False,
        kill_halted=True,
        dead_man_halted=True,
        notional_usd=Decimal("40"),
        wallet_value_usd=Decimal("80"),
        open_positions=3,
        asset_already_open=True,
        mint=_USDC,
        pool_liquidity_usd=None,
        freeze_authority_set=True,
        sell_route_ok=False,
    )
    result = _check(order, DayLedger(_DAY, halted=True, realized_loss_usd=Decimal("9")))
    assert result.accepted is True
    bad = replace(order, slippage_bps=80)
    assert _check(bad).code == "slippage_param"
    import inspect

    source = inspect.getsource(apply_kill)
    assert "apply_flatten" not in source
    assert "flatten(" not in source


def test_the_kill_file_fails_closed_and_needs_no_token(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    assert read_kill_state(missing) == "unreadable"
    assert kill_halted(missing) is True
    directory = tmp_path / "dir"
    directory.mkdir()
    assert read_kill_state(directory) == "unreadable"
    path = tmp_path / "kill"
    path.write_bytes(b"1")
    assert kill_halted(path) is True
    path.write_bytes(b"")
    assert kill_halted(path) is True
    path.write_bytes(b"0\n\n")
    assert kill_halted(path) is True
    path.write_bytes(b"0\n")
    assert kill_halted(path) is False
    path.write_bytes(b"0")
    assert kill_halted(path) is False
    assert kill_halted(path, {"LIVE_KILL": "1"}) is True
    assert kill_halted(environ={}) is True
    assert write_kill_halt(path) is True
    assert path.read_bytes() == b"1"
    assert kill_halted(path) is True
    text = Path(read_kill_state.__code__.co_filename).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "os", "pathlib", "collections"}
    assert "import sqlite" not in text
    assert "import fastapi" not in text
    assert "LIVE_CONTROL_TOKEN" not in text


def test_a_missed_scheduler_gap_halts_and_does_not_catch_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kill"
    path.write_bytes(b"0")
    monkeypatch.setenv("LIVE_KILL_FILE", str(path))
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    clock = {"now": 1_000}
    store = SqliteBarStore(tmp_path / "bars.sqlite", now=lambda: clock["now"])
    ticks = {"n": 0}

    class _Broker:
        def tick(self, *, hold: object = None) -> None:
            del hold
            ticks["n"] += 1

    class _Feed:
        def fetch(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("fetch")

        def close(self) -> None:
            return None

    try:
        scheduler = PaperScheduler(
            store,
            _Feed(),  # type: ignore[arg-type]
            now=lambda: clock["now"],
            interval=60,
            broker=_Broker(),  # type: ignore[arg-type]
        )
        scheduler.tick()
        assert ticks["n"] == 1
        assert scheduler.live_orders_for([1, 2, 3, 4, 5]) == (5,)
        assert path.read_bytes() == b"0"
        clock["now"] = 1_000 + 121
        scheduler.tick()
        assert ticks["n"] == 2
        assert scheduler.live_gate.dead_man is True
        assert path.read_bytes() == b"1"
        assert scheduler.live_orders_for([1, 2, 3, 4, 5]) == ()
    finally:
        store.close()


def test_consider_returns_only_the_newest_bar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    path = tmp_path / "kill"
    path.write_bytes(b"0")
    gate = LiveGate(kill_path=path)
    gate.note(now=10, interval=60)
    assert gate.consider([1, 2, 3, 4, 5]) == (5,)
    gate.note(now=10 + 121, interval=60)
    assert gate.dead_man is True
    assert gate.consider([1, 2, 3, 4, 5]) == ()
    assert path.read_bytes() == b"1"


def _quote() -> VenueQuote:
    return VenueQuote(
        SOL_MINT,
        _USDC,
        1_000,
        2_000,
        1_900,
        "-0.1",
        "Jupiter",
        "jupiter",
        VenueExpiry(500, None),
        "request",
        1_700_000_000,
        "buy",
        50,
        None,
        None,
        None,
        "ExactIn",
    )


def _swap(*extra: DecodedInstruction) -> tuple[DecodedInstruction, ...]:
    jupiter = DecodedInstruction(
        JUPITER_V6_PROGRAM,
        (
            AccountRole(SOL_MINT, False, False),
            AccountRole(_USDC, False, False),
        ),
        b"\x01",
    )
    return (*extra, jupiter)


def _assemble(
    instructions: tuple[DecodedInstruction, ...],
    *,
    payer: str = _WALLET,
    tables: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> UnsignedTransaction:
    return assemble_unsigned(
        payer=payer,
        instructions=instructions,
        lookup_tables=tables,
        recent_blockhash=b"\x11" * 32,
        quote=_quote(),
        last_valid_block_height=500,
    )


def _sim(*, spent: int = 1_000, gained: int = 1_900, drop: int = 5_000) -> Simulation:
    return Simulation(
        None,
        (
            TokenDelta(_POOL, SOL_MINT, _WALLET, 5_000, 5_000 - spent),
            TokenDelta(_TABLE, _USDC, _WALLET, 0, gained),
        ),
        (),
        (),
        ((_WALLET, 1_000_000),),
        ((_WALLET, 1_000_000 - drop),),
    )


def _guard(tx: UnsignedTransaction, rpc: MockChainRpc, **overrides: object) -> object:
    args: dict[str, object] = {
        "wallet": _WALLET,
        "owned": frozenset({_WALLET, _POOL, _TABLE}),
        "rpc": rpc,
        "quoted_in": 1_000,
        "quoted_out": 99_999,
        "quoted_minimum": 1_900,
        "input_account": _POOL,
        "output_account": _TABLE,
        "input_mint": SOL_MINT,
        "output_mint": _USDC,
    }
    args.update(overrides)
    return guard_unsigned(tx, **args)  # type: ignore[arg-type]


def test_the_guard_ignores_quote_copied_amounts_and_uses_the_simulation() -> None:
    limit = DecodedInstruction(COMPUTE_BUDGET_PROGRAM, (), compute_limit_data(200_000))
    built = _assemble(_swap(limit))
    tx = replace(built, quote_in_amount=1, quote_out_amount=1, quote_minimum_out_amount=1)
    rpc = MockChainRpc(height=10, simulation=_sim())
    decision = _guard(tx, rpc)
    assert decision.accepted is True
    assert decision.code == "ok"
    assert "lookup" not in str(rpc.calls)
    mismatch = _guard(tx, MockChainRpc(height=10, simulation=_sim(spent=500)))
    assert mismatch.code == "amount_mismatch"
    changed = Simulation(
        None,
        _sim().deltas,
        (AuthorityView(SOL_MINT, b"\x00" * 36, b"\x00" * 36),),
        (AuthorityView(SOL_MINT, b"\x01" * 36, b"\x00" * 36),),
        (),
        (),
    )
    assert _guard(tx, MockChainRpc(height=10, simulation=changed)).code == "authority_change"
    failed = replace(_sim(), err="simulation_failed")
    assert _guard(tx, MockChainRpc(height=10, simulation=failed)).code == "simulation_failed"
    costly = _guard(tx, MockChainRpc(height=10, simulation=_sim(drop=5_000_001)))
    assert costly.code == "per_tx_fee"
    missing = MockChainRpc(height=10, simulation=None)
    assert _guard(tx, missing).code == "no_simulation"


def test_structural_rejects_before_a_simulation() -> None:
    foreign = _assemble(_swap(DecodedInstruction(_PROGRAM, (), b"\x00")))
    assert _guard(foreign, MockChainRpc(height=1)).code == "program_id"
    nonce = _assemble(
        _swap(
            DecodedInstruction(
                SYSTEM_PROGRAM,
                (AccountRole(_WALLET, True, True), AccountRole(_WALLET, False, True)),
                advance_nonce_data(),
            )
        )
    )
    assert _guard(nonce, MockChainRpc(height=1)).code == "advance_nonce"
    other = _assemble(_swap(), payer=_OTHER)
    assert _guard(other, MockChainRpc(height=1)).code == "fee_payer"
    priced = _assemble(
        _swap(
            DecodedInstruction(COMPUTE_BUDGET_PROGRAM, (), compute_limit_data(200_000)),
            DecodedInstruction(COMPUTE_BUDGET_PROGRAM, (), compute_price_data(1_000_000)),
        )
    )
    assert _guard(priced, MockChainRpc(height=1)).code == "cu_cap"
    moved = _assemble(
        _swap(
            DecodedInstruction(
                SYSTEM_PROGRAM,
                (AccountRole(_WALLET, True, True), AccountRole(_OTHER, False, True)),
                system_transfer_data(1),
            )
        )
    )
    assert _guard(moved, MockChainRpc(height=1)).code == "destination"
    closed = _assemble(
        _swap(
            DecodedInstruction(
                TOKEN_PROGRAM,
                (
                    AccountRole(_POOL, False, True),
                    AccountRole(_OTHER, False, True),
                    AccountRole(_WALLET, True, True),
                ),
                bytes([TOKEN_CLOSE]),
            )
        )
    )
    assert _guard(closed, MockChainRpc(height=1)).code == "destination"
    approved = _assemble(
        _swap(
            DecodedInstruction(
                TOKEN_PROGRAM,
                (
                    AccountRole(_POOL, False, True),
                    AccountRole(_OTHER, False, True),
                    AccountRole(_WALLET, True, True),
                ),
                bytes([4]),
            )
        )
    )
    assert _guard(approved, MockChainRpc(height=1)).code == "destination"
    cleared = _assemble(
        _swap(
            DecodedInstruction(
                TOKEN_PROGRAM,
                (
                    AccountRole(_POOL, False, True),
                    AccountRole(_WALLET, True, True),
                ),
                bytes([6, 0, 0]),
            )
        )
    )
    assert _guard(cleared, MockChainRpc(height=1)).code == "destination"


def test_lookup_tables_must_match_the_chain_and_programs_stay_static() -> None:
    built = _assemble(
        (
            DecodedInstruction(
                JUPITER_V6_PROGRAM,
                (
                    AccountRole(SOL_MINT, False, False),
                    AccountRole(_USDC, False, True),
                ),
                b"\x01",
            ),
        ),
        tables=((_TABLE, (_USDC, _POOL)),),
    )
    assert built.lookup_tables
    wrong = LookupTable(_TABLE, True, (_OTHER, _POOL))
    rpc = MockChainRpc(height=10, tables={_TABLE: wrong}, simulation=_sim())
    assert _guard(built, rpc).code == "alt_mismatch"
    inactive = LookupTable(_TABLE, False, (_USDC, _POOL))
    assert _guard(built, MockChainRpc(height=10, tables={_TABLE: inactive})).code == "alt_inactive"
    assert _guard(built, MockChainRpc(height=10, tables={})).code == "alt_missing"
    crafted = _program_from_alt()
    table = LookupTable(_TABLE, True, (JUPITER_V6_PROGRAM,))
    chain = MockChainRpc(height=10, tables={_TABLE: table}, simulation=_sim())
    assert _guard(crafted, chain).code == "program_from_alt"


def _program_from_alt() -> UnsignedTransaction:
    blob = bytearray()
    blob.append(1)
    blob.extend(b"\x00" * 64)
    blob.append(0x80)
    blob.extend(bytes((1, 0, 1)))
    blob.append(2)
    blob.extend(address_bytes(_WALLET))
    blob.extend(address_bytes(SYSTEM_PROGRAM))
    blob.extend(b"\x11" * 32)
    blob.append(1)
    blob.append(2)
    blob.append(0)
    blob.append(0)
    blob.append(1)
    blob.extend(address_bytes(_TABLE))
    blob.append(1)
    blob.append(0)
    blob.append(0)
    raw = bytes(blob)
    view = decode_unsigned_v0(raw, ((_TABLE, (JUPITER_V6_PROGRAM,)),))
    assert view[2][0].program_id == JUPITER_V6_PROGRAM
    assert view[3] == 2
    return UnsignedTransaction(
        transaction=raw,
        instructions=view[2],
        fee_payer=view[1],
        taker=view[1],
        account_keys=view[0],
        static_account_count=view[3],
        lookup_tables=(
            ResolvedLookupTable(_TABLE, (JUPITER_V6_PROGRAM,), (0,), ()),
        ),
        input_mint=SOL_MINT,
        output_mint=_USDC,
        quote_in_amount=1,
        quote_out_amount=1,
        quote_minimum_out_amount=1,
        last_valid_block_height=0,
        transaction_version=0,
    )


def test_program_ids_decode_and_the_rpc_client_never_sends() -> None:
    for program in (
        SYSTEM_PROGRAM,
        COMPUTE_BUDGET_PROGRAM,
        TOKEN_PROGRAM,
        TOKEN_2022_PROGRAM,
        ASSOCIATED_TOKEN_PROGRAM,
        JUPITER_V6_PROGRAM,
        ADDRESS_LOOKUP_PROGRAM,
    ):
        assert len(address_of(program)) == 32
    header = (2**64 - 1).to_bytes(8, "little") + b"\x00" * 48
    raw = header + address_bytes(SOL_MINT)
    active, addresses = parse_lookup_table(raw)
    assert active is True
    assert addresses == (SOL_MINT,)
    inactive, _found = parse_lookup_table(b"\x00" * 56 + address_bytes(_USDC))
    assert inactive is False
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["method"])
        assert body["method"] != "sendTransaction"
        if body["method"] == "getBlockHeight":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 10})
        if body["method"] == "getAccountInfo":
            data = base64.b64encode(raw).decode("ascii")
            value = {"lamports": 1, "data": [data, "base64"]}
            result = {"context": {"slot": 1}, "value": value}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
        encoded = base64.b64encode(b"\x00" * 165).decode("ascii")
        accounts = [{"lamports": 1, "data": [encoded, "base64"]}]
        value = {"err": None, "accounts": accounts}
        result = {"context": {"slot": 1}, "value": value}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rpc = HttpChainRpc("http://127.0.0.1", client)
    assert rpc.block_height() == 10
    table = rpc.lookup_table(_TABLE)
    assert table is not None and table.active is True and table.addresses == (SOL_MINT,)
    simulated = rpc.simulate(b"\x00", [_POOL])
    assert simulated.err is None
    assert seen == ["getBlockHeight", "getAccountInfo", "getAccountInfo", "simulateTransaction"]
    with pytest.raises(Exception, match="send_disabled"):
        rpc._call("sendTransaction", [])
    assert "sendTransaction" not in rpc.methods
    assert default_rpc() is None


def test_quote_and_build_sends_the_risk_slippage_and_the_chain_height() -> None:
    seen: dict[str, object] = {}

    class _Venue(MockVenue):
        def build(  # type: ignore[override]
            self,
            quote: VenueQuote,
            *,
            taker: str,
            now: int,
            block_height: int | None = None,
            slippage_bps: int | None = None,
        ) -> UnsignedTransaction:
            seen["height"] = block_height
            seen["quote_height"] = quote.expiry.last_valid_block_height
            return super().build(
                quote,
                taker=taker,
                now=now,
                block_height=block_height,
                slippage_bps=slippage_bps,
            )

        def execute(self, transaction: object = None) -> None:
            seen["executed"] = True
            raise VenueExecuteDisabled("execute_disabled")

    venue = _Venue(
        input_mint=SOL_MINT,
        output_mint=_USDC,
        in_amount=1_000,
        out_amount=2_000,
        minimum_out_amount=1_900,
        price_impact="-0.1",
        route_label="Jupiter",
        router="jupiter",
        last_valid_block_height=500,
        instructions=_swap(),
        lookup_tables=(),
        recent_blockhash=b"\x11" * 32,
    )
    quote, built = quote_and_build(
        venue,
        input_mint=SOL_MINT,
        output_mint=_USDC,
        amount=1_000,
        side="buy",
        taker=_WALLET,
        now=1_700_000_000,
        rpc=MockChainRpc(height=10),
    )
    assert venue.last_quote_slippage == 50
    assert venue.last_build_slippage == 50
    assert seen["height"] == 10
    assert seen["quote_height"] == 500
    assert quote.expiry.last_valid_block_height == 500
    assert built.fee_payer == _WALLET
    assert "executed" not in seen
    with pytest.raises(VenueExecuteDisabled):
        venue.execute(built)
    with pytest.raises(VenueExecuteDisabled):
        JupiterVenue().execute()


def test_control_routes_require_host_origin_and_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kill"
    path.write_bytes(b"0")
    monkeypatch.setenv("LIVE_KILL_FILE", str(path))
    monkeypatch.setenv("LIVE_CONTROL_TOKEN", "test")
    calls = {"flatten": 0}

    def _count(*_args: object, **_kwargs: object) -> None:
        calls["flatten"] += 1
        raise AssertionError("flatten")

    monkeypatch.setattr("app.risk.routes.apply_flatten", _count)
    _freeze(_DAY)
    store = SqliteBarStore(tmp_path / "day.sqlite")
    app = create_app()
    app.state.live_day_store = store
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    headers = {"origin": "http://127.0.0.1", "x-live-token": "test"}
    pending_book().add(kind="submit", mint=SOL_MINT, side="buy")
    bad = {"origin": "https://evil.example", "x-live-token": "test"}
    denied = client.post("/live/kill", headers=bad)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "control_forbidden"
    assert path.read_bytes() == b"0"
    assert len(pending_book().open_rows()) == 1
    killed = client.post("/live/kill", headers=headers)
    assert killed.status_code == 200
    assert killed.json()["halted"] is True
    assert killed.json()["flattened"] is False
    assert killed.json()["sent"] is False
    assert killed.json()["canceled"] == 1
    assert path.read_bytes() == b"1"
    assert calls["flatten"] == 0
    assert pending_book().open_rows() == ()
    missing = client.post("/live/submit", json=_body(), headers={"origin": "http://127.0.0.1"})
    assert missing.status_code == 403
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    off = client.post("/live/submit", json=_body(), headers=headers)
    assert off.status_code == 409
    assert off.json()["code"] == "trading_disabled"
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    path.write_bytes(b"0")
    reset_process_halt()
    slipped = client.post("/live/submit", json=_body(slippage_bps=51), headers=headers)
    assert slipped.status_code == 422
    assert slipped.json()["code"] == "slippage_param"
    assert slipped.json()["resized"] is False
    bare = client.post("/live/submit", json=_body(), headers=headers)
    assert bare.status_code == 422
    assert bare.json()["code"] == "missing_transaction"
    assert bare.json()["sent"] is False
    assert pending_book().open_rows() == ()
    assert take_day(_DAY, store).notional_usd == Decimal("0")
    save_day(DayLedger(_DAY, notional_usd=Decimal("8")), store)
    over = client.post("/live/submit", json=_body(), headers=headers)
    assert over.json()["code"] == "daily_notional"
    again = client.post("/live/submit", json=_body(notional_usd="1"), headers=headers)
    assert again.json()["code"] == "daily_halt"
    monkeypatch.setattr("app.risk.routes.apply_flatten", apply_flatten_restore())
    flat = client.post("/live/flatten", json=_body(side="sell", action="flatten"), headers=headers)
    assert flat.status_code == 200
    assert flat.json()["flattened"] is True
    assert flat.json()["sent"] is False
    assert flat.json()["accepted"] is True
    assert host_allowed("evil.example") is False
    assert origin_allowed(None) is False
    assert origin_allowed("http://127.0.0.1") is True
    with pytest.raises(RuntimeError, match="loopback"):
        require_loopback("0.0.0.0")
    assert require_loopback(None) == "127.0.0.1"
    store.close()


def apply_flatten_restore():
    from app.risk.control import apply_flatten

    return apply_flatten


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "side": "buy",
        "action": "entry",
        "mint": SOL_MINT,
        "notional_usd": "2.50",
        "slippage_bps": 50,
        "price_impact": "-0.1",
        "pool_liquidity_usd": "100000",
        "pool_age_seconds": 200_000,
        "freeze_authority_set": False,
        "mint_authority_set": False,
        "sell_route_ok": True,
        "sell_route_impact": "0.1",
        "bar_age_bars": 0,
        "bar_price": "100",
        "quote_price": "100",
        "open_positions": 0,
        "asset_already_open": False,
        "network_fee_lamports": 5_000,
        "priority_fee_lamports": 1_000,
        "rent_lamports": 0,
        "wallet_value_usd": "20",
        "out_amount": 10_000,
        "minimum_out_amount": 9_950,
        "day": _DAY,
    }
    body.update(overrides)
    return body


def _freeze(day: str) -> None:
    year, month, dom = (int(part) for part in day.split("-"))
    stamp = int(datetime(year, month, dom, 15, tzinfo=UTC).timestamp())
    set_day_clock(lambda: stamp)


def test_a_future_day_cannot_reset_a_halted_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kill"
    path.write_bytes(b"0")
    monkeypatch.setenv("LIVE_KILL_FILE", str(path))
    monkeypatch.setenv("LIVE_CONTROL_TOKEN", "test")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    _freeze(_DAY)
    assert server_day() == _DAY
    store = SqliteBarStore(tmp_path / "day.sqlite")
    halted = DayLedger(
        _DAY,
        notional_usd=Decimal("10"),
        realized_loss_usd=Decimal("5"),
        halted=True,
    )
    save_day(halted, store)
    app = create_app()
    app.state.live_day_store = store
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234))
    headers = {"origin": "http://127.0.0.1", "x-live-token": "test"}
    try:
        future = client.post("/live/submit", json=_body(day="2026-11-26"), headers=headers)
        assert future.status_code == 422
        assert future.json()["accepted"] is False
        assert future.json()["code"] == "daily_halt"
        row = store.read_live_day()
        assert row is not None
        assert row[0] == _DAY
        assert Decimal(str(row[1])) == Decimal("10")
        assert Decimal(str(row[2])) == Decimal("5")
        assert row[7] == 1
        fresh = take_day(_DAY, store)
        again = evaluate(_order(), fresh, today=_DAY)
        assert again.accepted is False
        assert again.code == "daily_halt"
        reset_day_ledger()
        _freeze(_DAY)
        booked = SqliteBarStore(tmp_path / "booked.sqlite")
        save_day(DayLedger(_DAY, notional_usd=Decimal("2.50")), booked)
        app.state.live_day_store = booked
        missed = client.post("/live/submit", json=_body(day="2026-11-26"), headers=headers)
        assert missed.status_code == 422
        assert missed.json()["code"] == "missing_transaction"
        kept = booked.read_live_day()
        assert kept is not None
        assert kept[0] == _DAY
        assert Decimal(str(kept[1])) == Decimal("2.50")
        still = evaluate(_order(), take_day(_DAY, booked), today=_DAY)
        assert still.accepted is True
        assert still.ledger.notional_usd == Decimal("5.00")
        booked.close()
    finally:
        store.close()


def test_booked_notional_and_a_halt_survive_a_new_store(tmp_path: Path) -> None:
    path = tmp_path / "day.sqlite"
    store = SqliteBarStore(path)
    try:
        ledger = empty_day(_DAY)
        for _ in range(4):
            booked = evaluate(_order(), ledger, today=_DAY)
            assert booked.accepted is True
            ledger = booked.ledger
        assert ledger.notional_usd == Decimal("10")
        save_day(ledger, store)
        reset_day_ledger()
    finally:
        store.close()
    restarted = SqliteBarStore(path)
    try:
        loaded = take_day(_DAY, restarted)
        assert loaded.notional_usd == Decimal("10")
        assert loaded.halted is False
        rejected = evaluate(_order(), loaded, today=_DAY)
        assert rejected.accepted is False
        assert rejected.code == "daily_notional"
        save_day(
            DayLedger(
                _DAY,
                notional_usd=Decimal("10"),
                realized_loss_usd=Decimal("5"),
                network_lamports=7,
                halted=True,
            ),
            restarted,
        )
    finally:
        restarted.close()
    reset_day_ledger()
    again = SqliteBarStore(path)
    try:
        halted = take_day(_DAY, again)
        assert halted.halted is True
        assert halted.realized_loss_usd == Decimal("5")
        assert halted.network_lamports == 7
        refused = evaluate(_order(notional_usd=Decimal("1")), halted, today=_DAY)
        assert refused.code == "daily_halt"
        clock = {"now": 1_700_000_000}
        durable = SqliteBarStore(tmp_path / "purge.sqlite", now=lambda: clock["now"])
        save_day(halted, durable)
        clock["now"] += 90_000
        assert durable.paper_wallet(user_id=LOCAL_USER_ID, strategy_id="s", version=1) is None
        kept = durable.read_live_day()
        assert kept is not None and kept[0] == _DAY and kept[7] == 1
        durable.close()
    finally:
        again.close()


def test_a_wrong_mint_or_token2022_account_is_rejected() -> None:
    built = _assemble(_swap())
    junk = replace(
        _sim(),
        deltas=(
            TokenDelta(_POOL, SOL_MINT, _WALLET, 5_000, 4_000),
            TokenDelta(_TABLE, SYSTEM_PROGRAM, _WALLET, 0, 1_900),
        ),
    )
    assert _guard(built, MockChainRpc(height=10, simulation=junk)).code == "mint_mismatch"
    leaked = replace(
        _sim(),
        deltas=_sim().deltas + (TokenDelta(_OTHER, SOL_MINT, _WALLET, 20, 0),),
    )
    assert _guard(built, MockChainRpc(height=10, simulation=leaked)).code == "amount_mismatch"
    skipped = replace(_sim(), unparsed=(_POOL,))
    assert _guard(built, MockChainRpc(height=10, simulation=skipped)).code == "amount_unverified"
    assert classify_account(bytes(82)) == "mint"
    assert classify_account(bytes(83)) == "unparsed"
    assert classify_account(bytes(165)) == "token"
    assert classify_account(bytes(166)) == "unparsed"
    assert classify_account(b"") == "empty"
    named = _assemble(_swap(DecodedInstruction(TOKEN_2022_PROGRAM, (), b"\x00")))
    assert _guard(named, MockChainRpc(height=1)).code == "program_id"

    def _parsed(size: int, address: str) -> Simulation:
        raw = base64.b64encode(bytes(size)).decode("ascii")
        value = {"lamports": 1, "data": [raw, "base64"]}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body["method"] == "getAccountInfo":
                result = {"value": value}
            else:
                result = {"value": {"err": None, "accounts": [value]}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        return HttpChainRpc("http://127.0.0.1", client).simulate(b"\x00", [address])

    mint_2022 = _parsed(83, _POOL)
    token_2022 = _parsed(166, _TABLE)
    assert mint_2022.unparsed == (_POOL,)
    assert token_2022.unparsed == (_TABLE,)
    assert _guard(built, MockChainRpc(height=1, simulation=mint_2022)).code == "amount_unverified"
    assert _guard(built, MockChainRpc(height=1, simulation=token_2022)).code == "amount_unverified"
    assert _parsed(200, TOKEN_PROGRAM).unparsed == ()


def test_shadow_backlog_refuses_extra_work_without_waiting() -> None:
    from app.paper.shadow_queue import ShadowQueue

    release = threading.Event()
    started = threading.Event()

    def blocked(*_args: object, **_kwargs: object) -> None:
        started.set()
        release.wait(2)

    queue = ShadowQueue(timeout=5, backlog=1)
    # The worker calls observe_shadow. Patch it before the first submit starts the thread.
    import app.paper.shadow_queue as shadow_queue

    original = shadow_queue.observe_shadow
    shadow_queue.observe_shadow = blocked  # type: ignore[assignment]
    try:
        jobs = [_job() for _ in range(3)]
        first = queue.submit(jobs[0])
        assert started.wait(1)
        second = queue.submit(jobs[1])
        third = queue.submit(jobs[2])
        assert first is True
        assert second is True
        assert third is False
    finally:
        release.set()
        shadow_queue.observe_shadow = original
        queue.flush(2)


def _job() -> object:
    from app.paper.shadow_queue import ShadowJob

    return ShadowJob(
        store=object(),  # type: ignore[arg-type]
        user_id="local",
        strategy_id="s",
        version=1,
        document={},
        now=0,
        venue=object(),
        chain_rpc=None,
    )


def test_legacy_engine_and_runner_core_are_unchanged() -> None:
    root = Path(__file__).resolve().parents[1]
    legacy = root / "tests" / "legacy" / "backtest_engine_e56685c.py"
    runner = root / "app" / "runner" / "core.py"
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == _LEGACY
    assert hashlib.sha256(runner.read_bytes()).hexdigest() == _RUNNER
    risk = root / "app" / "risk"
    text = "\n".join(path.read_text(encoding="utf-8") for path in risk.rglob("*.py"))
    assert "import solders" not in text
    assert "import nacl" not in text
    assert "Keypair" not in text
    assert "ed25519" not in text
    assert process_halted() is False
