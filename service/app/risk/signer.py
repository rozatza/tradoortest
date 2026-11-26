"""Manual confirm, and the internal auto signer.

Prepare builds an unsigned swap, derives every risk input from the
quote, the decoded route, and a simulation, and holds the message
until it expires. Confirm checks the signed bytes against that message,
re-reads the chain height and the kill file, and only then submits.

The auto signer signs that same held message and then calls confirm.
It does not sign any other bytes. Guarded messages are single-use.
The bytes live in memory. They are not written to sqlite and they are
not logged.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal

from app.risk.bind import require_loopback
from app.risk.custody import SignerClosed, bind_guarded_lookup, load_signer, sign_guarded
from app.risk.engine import LiveOrder, evaluate
from app.risk.fees import FeeLines, record_realized_loss, save_day, server_day, take_day
from app.risk.gate import LiveGate
from app.risk.guard import guard_unsigned, simulated_min_out
from app.risk.killfile import kill_halted
from app.risk.limits import DEFAULT_LIMITS, RiskLimits, order_slippage_bps
from app.risk.order import quote_and_build
from app.risk.pending import pending_book
from app.risk.programs import JUPITER_V6_PROGRAM
from app.risk.route_ix import decode_jupiter_route
from app.risk.rpc import ChainRpc, Simulation, TokenDelta
from app.venue.errors import VenueError
from app.venue.flags import live_trading_enabled
from app.venue.tx import decode_unsigned_v0, message_fee_payer, split_transaction, zero_signatures
from app.venue.types import QuoteRequest

# Public mints and their decimals. USDC is the quote asset, not an entry.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_DECIMALS = {
    "So11111111111111111111111111111111111111112": 9,
    USDC_MINT: 6,
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": 6,
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": 9,
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": 5,
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": 6,
}
_PACKET = 1232


@dataclass(frozen=True, slots=True)
class LiveContext:
    """Server-side facts. None of these fields come from the request body."""

    venue: object
    rpc: ChainRpc
    marks: Mapping[str, Decimal]
    pools: Mapping[str, tuple[Decimal, int]]
    bar_age_bars: int
    bar_price: Decimal
    bar_seconds: int
    now: Callable[[], int]


@dataclass(frozen=True, slots=True)
class Intent:
    side: str
    action: str
    mint: str
    amount: int


@dataclass(frozen=True, slots=True)
class _Guarded:
    pending_id: int
    message: bytes
    message_hash: str
    wallet: str
    expires_at: int
    last_valid_block_height: int
    order: LiveOrder
    accounts: tuple[str, ...]
    in_usd: Decimal
    out_usd: Decimal
    quote_out: int
    output_account: str
    tables: tuple[tuple[str, tuple[str, ...]], ...]


class Step:
    def __init__(self, status: int, body: dict[str, object]) -> None:
        self.status = status
        self.body = body


class _Book:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[int, _Guarded] = {}

    def put(self, row: _Guarded) -> None:
        with self._lock:
            self._rows[row.pending_id] = row

    def take(self, pending_id: int) -> _Guarded | None:
        with self._lock:
            return self._rows.pop(pending_id, None)

    def peek(self, pending_id: int) -> _Guarded | None:
        with self._lock:
            return self._rows.get(pending_id)

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


_book = _Book()


def reset_guarded() -> None:
    """Test helper. Drops every in-memory guarded message."""
    _book.clear()


def clear_guarded() -> None:
    """Kill path. Unsigned bytes cannot be submitted after this."""
    _book.clear()


def prepare_trade(
    intent: Intent,
    *,
    context: LiveContext | None,
    gate: LiveGate,
    wallet: str | None,
    store: object | None,
    limits: RiskLimits | None = None,
    flattened: bool = False,
) -> Step:
    """Build, guard, and describe one unsigned trade. Does not submit."""
    chosen = DEFAULT_LIMITS if limits is None else limits
    closed = _closed(flattened)
    if closed is not None:
        return closed
    if wallet is None:
        return _step(409, "wallet_unconfigured", flattened=flattened)
    today = server_day()
    prior = take_day(today, store)
    if prior.halted and intent.action != "flatten":
        return _step(422, "daily_halt", flattened=flattened)
    if context is None or context.bar_seconds < 1:
        return _step(503, "unconfigured", flattened=flattened)
    if gate.dead_man and intent.action != "flatten":
        return _step(409, "dead_man", flattened=flattened)
    if any(row.mint == intent.mint for row in pending_book().open_rows()):
        return _step(409, "in_flight", flattened=flattened)
    try:
        now = _clock(context)
        pair = _pair(intent)
        if pair is None:
            return _step(422, "bad_side", flattened=flattened)
        quote, built = quote_and_build(
            context.venue,
            input_mint=pair[0],
            output_mint=pair[1],
            amount=intent.amount,
            side=intent.side,
            taker=wallet,
            now=now,
            rpc=context.rpc,
            limits=chosen,
        )
    except VenueError as exc:
        return _step(422, exc.code, flattened=flattened)
    if built.fee_payer != wallet:
        return _step(422, "fee_payer", flattened=flattened)
    try:
        height = context.rpc.block_height()
    except VenueError as exc:
        return _step(422, exc.code, flattened=flattened)
    if height > built.last_valid_block_height:
        return _step(422, "stale_block", flattened=flattened)
    try:
        discovered = context.rpc.simulate(built.transaction, built.account_keys)
    except VenueError as exc:
        return _step(422, exc.code, flattened=flattened)
    legs = _legs(discovered, wallet)
    if legs is None:
        return _step(422, "amount_unverified", flattened=flattened)
    spent, received = legs
    if intent.side == "buy" and received.mint not in chosen.allowlist:
        return _step(422, "allowlist", flattened=flattened)
    if spent.mint != quote.input_mint or received.mint != quote.output_mint:
        return _step(422, "mint_mismatch", flattened=flattened)
    if intent.side == "buy" and received.mint != intent.mint:
        return _step(422, "mint_mismatch", flattened=flattened)
    if intent.side == "sell" and spent.mint != intent.mint:
        return _step(422, "mint_mismatch", flattened=flattened)
    decision = guard_unsigned(
        built,
        wallet=wallet,
        owned=frozenset({wallet}),
        rpc=context.rpc,
        quoted_in=quote.in_amount,
        quoted_out=quote.out_amount,
        quoted_minimum=quote.minimum_out_amount,
        input_account=spent.account,
        output_account=received.account,
        input_mint=spent.mint,
        output_mint=received.mint,
        limits=chosen,
    )
    if not decision.accepted or decision.fees is None:
        return _step(422, decision.code, flattened=flattened)
    derived = _derive(
        intent,
        context,
        gate,
        wallet,
        spent,
        received,
        quote.out_amount,
        quote.minimum_out_amount,
        quote.price_impact,
        decision.fees,
        chosen,
    )
    if isinstance(derived, str):
        return _step(422, derived, flattened=flattened)
    order, in_usd, out_usd = derived
    result = evaluate(order, prior, chosen, today=today)
    if not result.accepted:
        if result.ledger.halted and not prior.halted:
            save_day(result.ledger, store)
        return _step(422, result.code, flattened=flattened)
    try:
        _sigs, message, count = split_transaction(built.transaction)
    except VenueError:
        return _step(422, "bad_transaction", flattened=flattened)
    if count != 1 or message_fee_payer(message) != wallet:
        return _step(422, "fee_payer", flattened=flattened)
    terms = _terms(built.instructions)
    if isinstance(terms, str):
        return _step(422, terms, flattened=flattened)
    expires = now + context.bar_seconds
    kind = "flatten" if flattened else "prepare"
    row = pending_book().add(kind=kind, mint=intent.mint, side=intent.side)
    _book.put(
        _Guarded(
            row.pending_id,
            message,
            hashlib.sha256(message).hexdigest(),
            wallet,
            expires,
            built.last_valid_block_height,
            order,
            built.account_keys,
            in_usd,
            out_usd,
            quote.out_amount,
            received.account,
            tuple((table.address, table.addresses) for table in built.lookup_tables),
        )
    )
    return Step(
        200,
        {
            "accepted": True,
            "code": "ok",
            "sent": False,
            "resized": False,
            "flattened": flattened,
            "pending_id": row.pending_id,
            "expires_at": expires,
            "transaction_b64": base64.b64encode(built.transaction).decode("ascii"),
            "trade": _display(
                order,
                decision.fees,
                terms,
                quote.price_impact,
                in_amount=spent.before - spent.after,
                out_amount=received.after - received.before,
            ),
        },
    )


_LOAD_CODES = frozenset(
    {
        "dead_man",
        "killed",
        "signer_disabled",
        "signer_key_length",
        "signer_key_malformed",
        "signer_key_unreadable",
        "signer_pubkey_mismatch",
        "signer_unconfigured",
        "trading_disabled",
        "wallet_unconfigured",
    }
)


def auto_trade(
    intent: Intent,
    *,
    context: LiveContext | None,
    gate: LiveGate,
    wallet: str | None,
    store: object | None,
    limits: RiskLimits | None = None,
    flattened: bool = False,
) -> Step:
    """Prepare, sign the guarded message, then confirm.

    Signing runs only when ``LIVE_SIGNER`` is ``local`` and live trading
    is exactly ``1``. The signature is the one ``sign_guarded`` produces
    for the stored message. Submit is ``confirm_trade``, which books the
    day before send.
    """
    if not live_trading_enabled():
        return _step(409, "trading_disabled", flattened=flattened)
    if os.environ.get("LIVE_SIGNER") != "local":
        return _step(409, "signer_disabled", flattened=flattened)
    try:
        require_loopback()
    except RuntimeError:
        return _step(409, "signer_disabled", flattened=flattened)
    prepared = prepare_trade(
        intent,
        context=context,
        gate=gate,
        wallet=wallet,
        store=store,
        limits=limits,
        flattened=flattened,
    )
    if prepared.status != 200:
        return prepared
    pending_raw = prepared.body.get("pending_id")
    if isinstance(pending_raw, bool) or not isinstance(pending_raw, int):
        return _step(422, "unknown_pending", flattened=flattened)
    pending_id = pending_raw
    item = _book.peek(pending_id)
    if item is None:
        return _step(422, "unguarded", flattened=flattened)
    try:
        held = load_signer()
        if kill_halted():
            raise SignerClosed("killed")
        if gate.dead_man and intent.action != "flatten":
            raise SignerClosed("dead_man")
        signature = sign_guarded(held, pending_id, item.message)
    except SignerClosed as exc:
        _consume(pending_id)
        status = 409 if exc.code in _LOAD_CODES else 422
        return _step(status, exc.code, flattened=flattened)
    signed = b"\x01" + signature + item.message
    encoded = base64.b64encode(signed).decode("ascii")
    return confirm_trade(
        pending_id,
        encoded,
        context=context,
        gate=gate,
        wallet=wallet,
        store=store,
        limits=limits,
    )


def confirm_trade(
    pending_id: int,
    signed_b64: str,
    *,
    context: LiveContext | None,
    gate: LiveGate,
    wallet: str | None,
    store: object | None,
    limits: RiskLimits | None = None,
) -> Step:
    """Re-check, match the message, then submit. One use."""
    chosen = DEFAULT_LIMITS if limits is None else limits
    if not live_trading_enabled():
        return _step(409, "trading_disabled")
    if kill_halted():
        return _step(409, "killed")
    if wallet is None:
        return _step(409, "wallet_unconfigured")
    if context is None:
        return _step(503, "unconfigured")
    item = _book.peek(pending_id)
    if item is None or not _still_pending(pending_id):
        return _step(422, "unknown_pending")
    try:
        now = _clock(context)
    except VenueError:
        return _step(422, "bad_clock")
    if now >= item.expires_at:
        _consume(pending_id)
        return _step(422, "expired")
    signed = _decode_signed(signed_b64)
    if signed is None:
        _consume(pending_id)
        return _step(422, "bad_transaction")
    try:
        sigs, message, count = split_transaction(signed)
        payer = message_fee_payer(message)
    except VenueError:
        _consume(pending_id)
        return _step(422, "bad_transaction")
    digest = hashlib.sha256(message).hexdigest()
    same_bytes = secrets.compare_digest(message, item.message)
    same_hash = secrets.compare_digest(digest, item.message_hash)
    if count != 1 or not same_bytes or not same_hash:
        _consume(pending_id)
        return _step(422, "byte_mismatch")
    if payer != wallet or item.wallet != wallet or sigs == b"\x00" * 64:
        _consume(pending_id)
        return _step(422, "fee_payer")
    try:
        height = context.rpc.block_height()
    except VenueError as exc:
        _consume(pending_id)
        return _step(422, exc.code)
    if height > item.last_valid_block_height:
        _consume(pending_id)
        return _step(422, "stale_block")
    today = server_day()
    order = replace(
        item.order,
        trading_enabled=live_trading_enabled(),
        kill_halted=kill_halted(),
        dead_man_halted=gate.dead_man,
    )
    try:
        simulated = context.rpc.simulate(signed, item.accounts, sig_verify=True)
    except VenueError as exc:
        _consume(pending_id)
        return _step(422, exc.code)
    if simulated.err is not None:
        _consume(pending_id)
        code = "bad_signature" if simulated.err == "bad_signature" else "simulation_failed"
        return _step(422, code)
    try:
        viewed = decode_unsigned_v0(zero_signatures(signed), item.tables)
    except VenueError:
        _consume(pending_id)
        return _step(422, "bad_transaction")
    gained = _output_gain(simulated, item.output_account, wallet)
    if gained is None:
        _consume(pending_id)
        return _step(422, "amount_unverified")
    bound = simulated_min_out(
        viewed[2],
        quote_out=item.quote_out,
        gained=gained,
        limits=chosen,
    )
    if bound is not None:
        _consume(pending_id)
        return _step(422, bound)
    result = evaluate(order, take_day(today, store), chosen, today=today)
    if not result.accepted:
        if result.ledger.halted:
            save_day(result.ledger, store)
        _consume(pending_id)
        return _step(422, result.code)
    if kill_halted():
        _consume(pending_id)
        return _step(409, "killed")
    if gate.dead_man and order.action != "flatten":
        _consume(pending_id)
        return _step(409, "dead_man")
    if not live_trading_enabled():
        _consume(pending_id)
        return _step(409, "trading_disabled")
    _consume(pending_id)
    save_day(result.ledger, store)
    try:
        signature = context.rpc.send(signed)
    except VenueError:
        return _step(503, "send_failed")
    try:
        status = context.rpc.confirm(signature)
    except VenueError:
        status = "unknown"
    confirmed = status == "confirmed"
    if confirmed:
        pnl = item.out_usd - item.in_usd
        booked = record_realized_loss(take_day(today, store), pnl, chosen)
        save_day(booked, store)
    return Step(
        200,
        {
            "accepted": True,
            "code": "ok" if confirmed else status,
            "sent": True,
            "resized": False,
            "signature": signature,
            "confirmed": confirmed,
        },
    )


def _closed(flattened: bool) -> Step | None:
    if not live_trading_enabled():
        return _step(409, "trading_disabled", flattened=flattened)
    if kill_halted():
        return _step(409, "killed", flattened=flattened)
    return None


def _step(status: int, code: str, *, flattened: bool = False) -> Step:
    body: dict[str, object] = {
        "accepted": False,
        "code": code,
        "sent": False,
        "resized": False,
    }
    if flattened:
        body["flattened"] = True
    return Step(status, body)


def _clock(context: LiveContext) -> int:
    now = context.now()
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise VenueError("bad_clock")
    return now


def _pair(intent: Intent) -> tuple[str, str] | None:
    if intent.side == "buy" and intent.action == "entry":
        return USDC_MINT, intent.mint
    if intent.side == "sell" and intent.action in ("exit", "flatten"):
        return intent.mint, USDC_MINT
    return None


def _output_gain(simulation: Simulation, account: str, wallet: str) -> int | None:
    """Atoms the output account gained. Missing or negative is refused."""
    for item in simulation.deltas:
        if item.account != account or item.owner != wallet:
            continue
        gain = item.after - item.before
        if gain < 0:
            return None
        return gain
    return None


def _legs(simulation: Simulation, wallet: str) -> tuple[TokenDelta, TokenDelta] | None:
    if simulation.err is not None or simulation.unparsed:
        return None
    spent: list[TokenDelta] = []
    received: list[TokenDelta] = []
    for item in simulation.deltas:
        if item.owner != wallet:
            continue
        if item.before > item.after:
            spent.append(item)
        elif item.after > item.before:
            received.append(item)
    if len(spent) != 1 or len(received) != 1:
        return None
    return spent[0], received[0]


def _derive(
    intent: Intent,
    context: LiveContext,
    gate: LiveGate,
    wallet: str,
    spent: TokenDelta,
    received: TokenDelta,
    out_amount: int,
    minimum_out: int,
    price_impact: str,
    fees: FeeLines,
    limits: RiskLimits,
) -> tuple[LiveOrder, Decimal, Decimal] | str:
    in_amount = spent.before - spent.after
    gained = received.after - received.before
    in_usd = _usd(spent.mint, in_amount, context.marks)
    out_usd = _usd(received.mint, gained, context.marks)
    quote_price = _price(intent.side, in_usd, out_usd, spent, received)
    wallet_value, open_positions, held, priced = _wallet(context, wallet, limits)
    mark_ok = bool(
        priced
        and in_usd is not None
        and out_usd is not None
        and quote_price is not None
        and context.bar_price > 0
    )
    mint_set, freeze_set = _authorities(context.rpc, received.mint)
    sell_ok, sell_impact = (True, "0")
    if intent.side == "buy":
        sell_ok, sell_impact = _sell_back(context, received.mint, gained, limits)
    pool = context.pools.get(received.mint if intent.side == "buy" else spent.mint)
    asset = received.mint if intent.side == "buy" else spent.mint
    order = LiveOrder(
        side=intent.side,
        action=intent.action,
        mint=asset,
        notional_usd=in_usd,
        slippage_bps=order_slippage_bps(limits),
        price_impact=price_impact,
        pool_liquidity_usd=None if pool is None else pool[0],
        pool_age_seconds=None if pool is None else pool[1],
        freeze_authority_set=freeze_set,
        mint_authority_set=mint_set,
        sell_route_ok=sell_ok,
        sell_route_impact=sell_impact,
        bar_age_bars=context.bar_age_bars,
        bar_price=context.bar_price,
        quote_price=quote_price,
        open_positions=open_positions,
        asset_already_open=asset in held,
        fees=fees,
        wallet_value_usd=wallet_value,
        mark_ok=mark_ok,
        trading_enabled=live_trading_enabled(),
        kill_halted=kill_halted(),
        dead_man_halted=gate.dead_man,
        out_amount=out_amount,
        minimum_out_amount=minimum_out,
    )
    if in_usd is None or out_usd is None:
        return order, Decimal("0"), Decimal("0")
    return order, in_usd, out_usd


def _usd(mint: str, amount: int, marks: Mapping[str, Decimal]) -> Decimal | None:
    decimals = _DECIMALS.get(mint)
    mark = marks.get(mint)
    if decimals is None or mark is None or amount < 0:
        return None
    if not isinstance(mark, Decimal) or not mark.is_finite() or mark <= 0:
        return None
    return (Decimal(amount) / (Decimal(10) ** decimals)) * mark


def _price(
    side: str,
    in_usd: Decimal | None,
    out_usd: Decimal | None,
    spent: TokenDelta,
    received: TokenDelta,
) -> Decimal | None:
    if side == "buy":
        usd = in_usd
        amount = received.after - received.before
        mint = received.mint
    else:
        usd = out_usd
        amount = spent.before - spent.after
        mint = spent.mint
    decimals = _DECIMALS.get(mint)
    if usd is None or decimals is None or amount <= 0:
        return None
    tokens = Decimal(amount) / (Decimal(10) ** decimals)
    if tokens <= 0:
        return None
    return usd / tokens


def _wallet(
    context: LiveContext,
    wallet: str,
    limits: RiskLimits,
) -> tuple[Decimal | None, int, frozenset[str], bool]:
    try:
        info = context.rpc.account(wallet)
        rows = context.rpc.holdings(wallet)
    except VenueError:
        return None, 0, frozenset(), False
    if info is None:
        return None, 0, frozenset(), False
    sol = _usd(_SOL, info[0], context.marks)
    if sol is None:
        return None, 0, frozenset(), False
    total = sol
    held: set[str] = set()
    for _account, mint, amount in rows:
        if amount <= 0:
            continue
        part = _usd(mint, amount, context.marks)
        if part is None:
            return None, 0, frozenset(), False
        total += part
        if mint in limits.allowlist:
            held.add(mint)
    return total, len(held), frozenset(held), True


_SOL = "So11111111111111111111111111111111111111112"


def _authorities(rpc: ChainRpc, mint: str) -> tuple[bool | None, bool | None]:
    try:
        info = rpc.account(mint)
    except VenueError:
        return None, None
    if info is None or len(info[1]) != 82:
        return None, None
    data = info[1]
    mint_tag = int.from_bytes(data[0:4], "little")
    freeze_tag = int.from_bytes(data[46:50], "little")
    if mint_tag not in (0, 1) or freeze_tag not in (0, 1):
        return None, None
    return mint_tag == 1, freeze_tag == 1


def _sell_back(
    context: LiveContext,
    mint: str,
    amount: int,
    limits: RiskLimits,
) -> tuple[bool, str | None]:
    if amount <= 0:
        return False, None
    request = QuoteRequest(mint, USDC_MINT, amount, "sell", order_slippage_bps(limits))
    try:
        quote = context.venue.quote(request, now=_clock(context))  # type: ignore[attr-defined]
    except VenueError:
        return False, None
    return True, quote.price_impact


def _terms(instructions: tuple[object, ...]) -> object:
    found = [
        item for item in instructions if getattr(item, "program_id", None) == JUPITER_V6_PROGRAM
    ]
    if len(found) != 1:
        return "route_undecoded"
    decoded = decode_jupiter_route(found[0].data)  # type: ignore[attr-defined]
    if isinstance(decoded, str):
        return decoded
    if decoded is None:
        return "route_undecoded"
    return decoded


def _display(
    order: LiveOrder,
    fees: FeeLines,
    terms: object,
    price_impact: str,
    *,
    in_amount: int,
    out_amount: int,
) -> dict[str, object]:
    notional = order.notional_usd
    return {
        "input_mint": USDC_MINT if order.side == "buy" else order.mint,
        "output_mint": order.mint if order.side == "buy" else USDC_MINT,
        "in_amount": in_amount,
        "out_amount": out_amount,
        "minimum_out_amount": getattr(terms, "min_out", order.minimum_out_amount),
        "slippage_bps": getattr(terms, "slippage_bps", order.slippage_bps),
        "price_impact": price_impact,
        "network_fee_lamports": fees.network_lamports,
        "priority_fee_lamports": fees.priority_lamports,
        "rent_lamports": fees.rent_lamports,
        "venue_fee_amount": fees.venue_fee_amount,
        "notional_usd": None if notional is None else format(notional, "f"),
        "limits_code": "ok",
    }


def _still_pending(pending_id: int) -> bool:
    return any(
        row.pending_id == pending_id and row.status == "pending"
        for row in pending_book().open_rows()
    )


def _consume(pending_id: int) -> None:
    pending_book().finish(pending_id, "consumed")
    _book.take(pending_id)


def _guarded_record(pending_id: int) -> tuple[bytes, str] | None:
    item = _book.peek(pending_id)
    if item is None or item.pending_id != pending_id or not _still_pending(pending_id):
        return None
    return item.message, item.message_hash


bind_guarded_lookup(_guarded_record)


def _decode_signed(value: str) -> bytes | None:
    if not isinstance(value, str) or value == "" or len(value) > 4_096:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    if not raw or len(raw) > _PACKET:
        return None
    return raw
