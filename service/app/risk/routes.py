"""Loopback routes for kill, flatten, and manual confirm.

Prepare builds an unsigned transaction and returns it for the browser
wallet. Confirm submits only after the signed bytes match the guarded
message. Neither route holds a key. ``apply_flatten`` stays imported so
the kill path can be shown not to call it.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.ohlcv.store import SqliteBarStore
from app.paper.routes import _bar_store
from app.risk.access import ControlRejected, require_control
from app.risk.control import KillPersistError, apply_flatten, apply_kill
from app.risk.gate import LiveGate
from app.risk.pending import pending_book
from app.risk.signer import Intent, LiveContext, confirm_trade, prepare_trade
from app.risk.wallet import configured_wallet
from app.venue.address import require_address
from app.venue.errors import VenueError

router = APIRouter()


def flatten_hook() -> object:
    """Bound so tests can patch ``apply_flatten``. Kill does not call it."""
    return apply_flatten


class IntentIn(BaseModel):
    """Which trade to build. Risk numbers are not fields here."""

    model_config = ConfigDict(extra="forbid")

    side: str
    action: str
    mint: str
    amount: int = Field(gt=0, lt=2**64)


class ConfirmIn(BaseModel):
    """The wallet's signed bytes for one guarded message."""

    model_config = ConfigDict(extra="forbid")

    pending_id: int = Field(gt=0)
    signed_transaction_b64: str


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
async def flatten(request: Request, body: IntentIn) -> JSONResponse:
    """Explicit flatten. Builds an unsigned sell. Does not submit it."""
    try:
        require_control(request)
    except ControlRejected:
        return _forbidden()
    if body.action != "flatten" or body.side != "sell":
        return JSONResponse(
            status_code=422,
            content={"accepted": False, "code": "bad_side", "sent": False, "resized": False},
        )
    return _prepare(request, body, flattened=True)


@router.post("/live/prepare")
async def prepare(request: Request, body: IntentIn) -> JSONResponse:
    """Build and guard an unsigned swap. The response is what the wallet signs."""
    try:
        require_control(request)
    except ControlRejected:
        return _forbidden()
    return _prepare(request, body, flattened=False)


@router.post("/live/submit")
async def submit(request: Request, body: IntentIn) -> JSONResponse:
    """Same as prepare. This route does not submit."""
    try:
        require_control(request)
    except ControlRejected:
        return _forbidden()
    return _prepare(request, body, flattened=False)


@router.post("/live/confirm")
async def confirm(request: Request, body: ConfirmIn) -> JSONResponse:
    """Match the guarded message, re-check, then submit."""
    try:
        require_control(request)
    except ControlRejected:
        return _forbidden()
    store = _day_store(request)
    if isinstance(store, JSONResponse):
        return store
    step = confirm_trade(
        body.pending_id,
        body.signed_transaction_b64,
        context=_context(request),
        gate=_gate(request),
        wallet=configured_wallet(),
        store=store,
    )
    return JSONResponse(status_code=step.status, content=step.body)


def _prepare(request: Request, body: IntentIn, *, flattened: bool) -> JSONResponse:
    try:
        mint = require_address(body.mint)
    except VenueError:
        return JSONResponse(
            status_code=422,
            content={
                "accepted": False,
                "code": "invalid_address",
                "sent": False,
                "resized": False,
                "flattened": flattened,
            },
        )
    if isinstance(body.amount, bool):
        return JSONResponse(
            status_code=422,
            content={"accepted": False, "code": "bad_amount", "sent": False, "resized": False},
        )
    store = _day_store(request)
    if isinstance(store, JSONResponse):
        return store
    step = prepare_trade(
        Intent(body.side, body.action, mint, body.amount),
        context=_context(request),
        gate=_gate(request),
        wallet=configured_wallet(),
        store=store,
        flattened=flattened,
    )
    return JSONResponse(status_code=step.status, content=step.body)


def _context(request: Request) -> LiveContext | None:
    current = getattr(request.app.state, "live_context", None)
    if isinstance(current, LiveContext):
        return current
    return None


def _gate(request: Request) -> LiveGate:
    """The scheduler's dead-man clock, or a gate that starts clear."""
    current = getattr(request.app.state, "live_gate", None)
    if isinstance(current, LiveGate):
        return current
    scheduler = getattr(request.app.state, "live_scheduler", None)
    shared = getattr(scheduler, "live_gate", None)
    if isinstance(shared, LiveGate):
        request.app.state.live_gate = shared
        return shared
    created = LiveGate()
    request.app.state.live_gate = created
    return created


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

