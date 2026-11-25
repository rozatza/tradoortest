"""Loopback control routes. They do not sign and they do not submit."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.ohlcv.store import SqliteBarStore
from app.paper.routes import _bar_store
from app.risk.access import ControlRejected, require_control
from app.risk.control import KillPersistError, apply_flatten, apply_kill
from app.risk.engine import LiveOrder, evaluate
from app.risk.fees import FeeLines, save_day, server_day, take_day
from app.risk.guard import guard_unsigned
from app.risk.killfile import kill_halted
from app.risk.limits import DEFAULT_LIMITS
from app.risk.pending import pending_book
from app.venue.flags import live_trading_enabled
from app.venue.tx import decode_unsigned_v0
from app.venue.types import UnsignedTransaction

router = APIRouter()


class SubmitIn(BaseModel):
    """One live check. Extra fields are rejected.

    The ledger day is the server's UTC date. The body does not carry one.
    """

    model_config = ConfigDict(extra="forbid")

    side: str
    action: str
    mint: str
    notional_usd: str
    slippage_bps: int
    price_impact: str
    pool_liquidity_usd: str | None = None
    pool_age_seconds: int | None = None
    freeze_authority_set: bool | None = None
    mint_authority_set: bool | None = None
    sell_route_ok: bool | None = None
    sell_route_impact: str | None = None
    bar_age_bars: int
    bar_price: str
    quote_price: str
    open_positions: int
    asset_already_open: bool
    network_fee_lamports: int
    priority_fee_lamports: int
    rent_lamports: int
    wallet_value_usd: str
    out_amount: int
    minimum_out_amount: int
    input_mint: str | None = None
    output_mint: str | None = None
    in_amount: int = 0
    transaction_b64: str | None = None
    input_account: str | None = None
    output_account: str | None = None
    owned_accounts: list[str] = []


def _forbidden() -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": {"code": "control_forbidden"}})


@router.post("/live/kill")
async def kill(request: Request) -> JSONResponse:
    """Halt within this call and cancel pending. Does not flatten."""
    try:
        require_control(request)
    except ControlRejected:
        return _forbidden()
    try:
        canceled = apply_kill()
    except KillPersistError:
        return JSONResponse(
            status_code=503,
            content={"halted": True, "canceled": pending_book().cancel_all(), "flattened": False},
        )
    return JSONResponse(
        {"halted": True, "canceled": canceled, "flattened": False, "sent": False}
    )


@router.post("/live/flatten")
async def flatten(request: Request, body: SubmitIn) -> JSONResponse:
    """Explicit flatten. Kill does not call this route."""
    try:
        require_control(request)
    except ControlRejected:
        return _forbidden()
    if body.action != "flatten" or body.side != "sell":
        return JSONResponse(status_code=422, content={"error": {"code": "bad_side"}})
    if _negative_fee_body(body):
        return JSONResponse(
            status_code=422,
            content={"accepted": False, "code": "per_tx_fee", "sent": False, "resized": False},
        )
    store = _day_store(request)
    if isinstance(store, JSONResponse):
        return store
    today = server_day()
    result = apply_flatten(_order(body), take_day(today, store), today=today)
    save_day(result.ledger, store)
    return JSONResponse(
        {
            "accepted": result.accepted,
            "code": result.code,
            "flattened": True,
            "sent": False,
            "resized": False,
        }
    )


@router.post("/live/submit")
async def submit(request: Request, body: SubmitIn) -> JSONResponse:
    """Risk-check and guard an unsigned transaction. Does not send it."""
    try:
        require_control(request)
    except ControlRejected:
        return _forbidden()
    if not live_trading_enabled():
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "code": "trading_disabled", "sent": False},
        )
    if kill_halted():
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "code": "killed", "sent": False},
        )
    if _negative_fee_body(body):
        return JSONResponse(
            status_code=422,
            content={"accepted": False, "code": "per_tx_fee", "sent": False, "resized": False},
        )
    store = _day_store(request)
    if isinstance(store, JSONResponse):
        return store
    today = server_day()
    prior = take_day(today, store)
    result = evaluate(_order(body), prior, today=today)
    if not result.accepted:
        save_day(result.ledger, store)
        return JSONResponse(
            status_code=422,
            content={"accepted": False, "code": result.code, "sent": False, "resized": False},
        )
    guarded = _guard(request, body)
    if guarded is not None:
        return JSONResponse(
            status_code=422,
            content={"accepted": False, "code": guarded, "sent": False},
        )
    save_day(result.ledger, store)
    row = pending_book().add(kind="submit", mint=body.mint, side=body.side)
    return JSONResponse(
        {
            "accepted": True,
            "code": "ok",
            "sent": False,
            "pending_id": row.pending_id,
            "resized": False,
        }
    )


def _order(body: SubmitIn) -> LiveOrder:
    return LiveOrder(
        side=body.side,
        action=body.action,
        mint=body.mint,
        notional_usd=_decimal(body.notional_usd),
        slippage_bps=body.slippage_bps,
        price_impact=body.price_impact,
        pool_liquidity_usd=_optional_decimal(body.pool_liquidity_usd),
        pool_age_seconds=body.pool_age_seconds,
        freeze_authority_set=body.freeze_authority_set,
        mint_authority_set=body.mint_authority_set,
        sell_route_ok=body.sell_route_ok,
        sell_route_impact=body.sell_route_impact,
        bar_age_bars=body.bar_age_bars,
        bar_price=_decimal(body.bar_price),
        quote_price=_decimal(body.quote_price),
        open_positions=body.open_positions,
        asset_already_open=body.asset_already_open,
        fees=FeeLines(
            body.network_fee_lamports,
            body.priority_fee_lamports,
            body.rent_lamports,
        ),
        wallet_value_usd=_decimal(body.wallet_value_usd),
        mark_ok=True,
        trading_enabled=live_trading_enabled(),
        kill_halted=kill_halted(),
        dead_man_halted=False,
        out_amount=body.out_amount,
        minimum_out_amount=body.minimum_out_amount,
    )


def _guard(request: Request, body: SubmitIn) -> str | None:
    if body.transaction_b64 is None or body.input_account is None or body.output_account is None:
        return "missing_transaction"
    input_mint = body.input_mint
    output_mint = body.output_mint
    if input_mint is None or output_mint is None or input_mint == output_mint:
        return "mint_mismatch"
    import base64

    try:
        raw = base64.b64decode(body.transaction_b64, validate=True)
        view = decode_unsigned_v0(raw, ())
    except Exception:
        return "bad_transaction"
    tx = UnsignedTransaction(
        transaction=raw,
        instructions=view[2],
        fee_payer=view[1],
        taker=view[1],
        account_keys=view[0],
        static_account_count=view[3],
        lookup_tables=view[4],
        input_mint=input_mint,
        output_mint=output_mint,
        quote_in_amount=body.in_amount,
        quote_out_amount=body.out_amount,
        quote_minimum_out_amount=body.minimum_out_amount,
        last_valid_block_height=0,
        transaction_version=0,
    )
    rpc = getattr(request.app.state, "chain_rpc", None)
    decision = guard_unsigned(
        tx,
        wallet=view[1],
        owned=frozenset(body.owned_accounts),
        rpc=rpc,
        quoted_in=body.in_amount,
        quoted_out=body.out_amount,
        quoted_minimum=body.minimum_out_amount,
        input_account=body.input_account,
        output_account=body.output_account,
        input_mint=input_mint,
        output_mint=output_mint,
        limits=DEFAULT_LIMITS,
    )
    if decision.accepted:
        return None
    return decision.code


def _day_store(request: Request) -> SqliteBarStore | JSONResponse:
    """The bar-store file. Tests may set ``app.state.live_day_store``."""
    current = getattr(request.app.state, "live_day_store", None)
    if isinstance(current, SqliteBarStore):
        return current
    try:
        return _bar_store(request)
    except (OSError, sqlite3.Error, ValueError):
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "store_unavailable"}},
        )


def _negative_fee_body(body: SubmitIn) -> bool:
    parts = (body.network_fee_lamports, body.priority_fee_lamports, body.rent_lamports)
    return any(isinstance(part, bool) or not isinstance(part, int) or part < 0 for part in parts)


def _decimal(text: str) -> Decimal | None:
    """A finite decimal. A negative number is rejected here, before it is booked."""
    try:
        number = Decimal(text)
    except Exception:
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def _optional_decimal(text: str | None) -> Decimal | None:
    if text is None:
        return None
    return _decimal(text)
