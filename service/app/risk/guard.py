"""Pre-sign checks on an unsigned version-0 transaction.

The quote's in, out, and minimum amounts are not read off the
transaction. Those fields are copies of the quote. Outer token
transfers are decoded. Every transaction is also simulated, and the
balance delta plus the mint and freeze authorities are checked. This
module does not sign and does not submit.
"""

from __future__ import annotations

import struct

from app.risk.fees import FeeLines, split_lamport_drop
from app.risk.limits import DEFAULT_LIMITS, RiskLimits
from app.risk.programs import (
    ALLOWED_PROGRAMS,
    ASSOCIATED_TOKEN_PROGRAM,
    COMPUTE_BUDGET_PROGRAM,
    CU_LIMIT,
    CU_PRICE,
    JUPITER_V6_PROGRAM,
    SYSTEM_ADVANCE_NONCE,
    SYSTEM_PROGRAM,
    SYSTEM_TRANSFER,
    TOKEN_ALLOWED,
    TOKEN_APPROVE,
    TOKEN_APPROVE_CHECKED,
    TOKEN_CLOSE,
    TOKEN_PROGRAMS,
    TOKEN_SET_AUTHORITY,
    TOKEN_SYNC_NATIVE,
    TOKEN_TRANSFER,
    TOKEN_TRANSFER_CHECKED,
)
from app.risk.route_ix import RouteTerms, decode_jupiter_route
from app.risk.rpc import ChainRpc, Simulation
from app.venue.address import address_bytes
from app.venue.errors import VenueError
from app.venue.tx import decode_unsigned_v0
from app.venue.types import AccountRole, DecodedInstruction, UnsignedTransaction


class GuardDecision:
    """A rejected transaction is not resized and not signed.

    ``fees`` is set only when the simulation's wallet lamport drop was
    split into network, priority, and rent. It is not a client number.
    """

    def __init__(self, accepted: bool, code: str, fees: FeeLines | None = None) -> None:
        self.accepted = accepted
        self.code = code
        self.fees = fees

    def __repr__(self) -> str:
        return f"GuardDecision({self.accepted!r}, {self.code!r})"


def guard_unsigned(
    tx: UnsignedTransaction,
    *,
    wallet: str,
    owned: frozenset[str],
    rpc: ChainRpc | None,
    quoted_in: int,
    quoted_out: int,
    quoted_minimum: int,
    input_account: str,
    output_account: str,
    input_mint: str,
    output_mint: str,
    limits: RiskLimits | None = None,
) -> GuardDecision:
    """Check ``tx.transaction``. Copied quote fields on ``tx`` are ignored.

    ``quoted_out`` is the venue quote. The route must name that amount,
    and its min-out must cover the simulated gain at the slippage cap.
    """
    chosen = DEFAULT_LIMITS if limits is None else limits
    known = set(owned)
    known.add(wallet)
    try:
        view = _view(tx, rpc)
    except VenueError as exc:
        return GuardDecision(False, exc.code)
    if isinstance(view, GuardDecision):
        return view
    keys, fee_payer, instructions, static_count, _tables = view
    if fee_payer != wallet or not keys or keys[0] != wallet:
        return GuardDecision(False, "fee_payer")
    structural = _structural(instructions, keys, static_count, wallet, known, chosen)
    if structural is not None:
        return GuardDecision(False, structural)
    decoded = _decoded_transfer_amount(instructions)
    if decoded is not None and decoded != quoted_in:
        return GuardDecision(False, "amount_mismatch")
    route = _route_terms(instructions, quoted_in, quoted_out, quoted_minimum, chosen)
    if isinstance(route, str):
        return GuardDecision(False, route)
    if rpc is None:
        return GuardDecision(False, "missing_rpc")
    try:
        simulation = rpc.simulate(tx.transaction, keys)
    except VenueError as exc:
        return GuardDecision(False, exc.code)
    return _simulated(
        simulation,
        wallet,
        quoted_in,
        quoted_out,
        quoted_minimum,
        input_account,
        output_account,
        input_mint,
        output_mint,
        chosen,
        instructions,
    )


def _view(
    tx: UnsignedTransaction,
    rpc: ChainRpc | None,
) -> tuple[object, ...] | GuardDecision:
    claimed = tuple((table.address, table.addresses) for table in tx.lookup_tables)
    if not claimed:
        return decode_unsigned_v0(tx.transaction, ())
    if rpc is None:
        return GuardDecision(False, "missing_rpc")
    chain: list[tuple[str, tuple[str, ...]]] = []
    for address, _contents in claimed:
        found = rpc.lookup_table(address)
        if found is None:
            return GuardDecision(False, "alt_missing")
        if not found.active:
            return GuardDecision(False, "alt_inactive")
        chain.append((address, found.addresses))
    chain_view = decode_unsigned_v0(tx.transaction, chain)
    claimed_view = decode_unsigned_v0(tx.transaction, claimed)
    if chain_view[0] != claimed_view[0]:
        return GuardDecision(False, "alt_mismatch")
    return chain_view


def _structural(
    instructions: tuple[DecodedInstruction, ...],
    keys: tuple[str, ...],
    static_count: int,
    wallet: str,
    owned: set[str],
    limits: RiskLimits,
) -> str | None:
    cu_limit: int | None = None
    cu_price: int | None = None
    for instruction in instructions:
        if instruction.program_id not in ALLOWED_PROGRAMS:
            return "program_id"
        index = _index(keys, instruction.program_id)
        if index is None or index >= static_count:
            return "program_from_alt"
        for account in instruction.accounts:
            if account.signer and account.pubkey != wallet:
                return "extra_signer"
        code = _one(instruction, wallet, owned)
        if code is not None:
            return code
        if instruction.program_id == COMPUTE_BUDGET_PROGRAM:
            parsed = _compute(instruction.data)
            if parsed is None:
                return "compute_budget"
            kind, value = parsed
            if kind == "limit":
                cu_limit = value
            else:
                cu_price = value
    return _cu_cap(cu_limit, cu_price, limits)


def _one(instruction: DecodedInstruction, wallet: str, owned: set[str]) -> str | None:
    program = instruction.program_id
    if program == SYSTEM_PROGRAM:
        return _system(instruction.data, instruction.accounts, wallet, owned)
    if program in TOKEN_PROGRAMS:
        return _token(instruction.data, instruction.accounts, wallet, owned)
    if program == ASSOCIATED_TOKEN_PROGRAM:
        if len(instruction.accounts) < 3 or instruction.accounts[2].pubkey != wallet:
            return "owner"
        owned.add(instruction.accounts[1].pubkey)
        return None
    if program in (JUPITER_V6_PROGRAM, COMPUTE_BUDGET_PROGRAM):
        return None
    return "program_id"


def _system(
    data: bytes,
    accounts: tuple[AccountRole, ...],
    wallet: str,
    owned: set[str],
) -> str | None:
    if len(data) < 4:
        return "instruction"
    discriminator = struct.unpack_from("<I", data)[0]
    if discriminator == SYSTEM_ADVANCE_NONCE:
        return "advance_nonce"
    if discriminator != SYSTEM_TRANSFER:
        return "instruction"
    if len(accounts) < 2 or len(data) < 12:
        return "instruction"
    if accounts[0].pubkey != wallet:
        return "owner"
    if accounts[1].pubkey not in owned:
        return "destination"
    return None


def _token(
    data: bytes,
    accounts: tuple[AccountRole, ...],
    wallet: str,
    owned: set[str],
) -> str | None:
    if not data:
        return "instruction"
    discriminator = data[0]
    if discriminator not in TOKEN_ALLOWED:
        return "instruction"
    if discriminator == TOKEN_SYNC_NATIVE:
        return None
    if discriminator in (TOKEN_TRANSFER, TOKEN_TRANSFER_CHECKED):
        checked = discriminator == TOKEN_TRANSFER_CHECKED
        dest = 2 if checked else 1
        authority = 3 if checked else 2
        if len(accounts) <= authority:
            return "instruction"
        if accounts[authority].pubkey != wallet:
            return "owner"
        if accounts[dest].pubkey not in owned:
            return "destination"
        return None
    if discriminator in (TOKEN_APPROVE, TOKEN_APPROVE_CHECKED):
        checked = discriminator == TOKEN_APPROVE_CHECKED
        delegate = 2 if checked else 1
        owner = 3 if checked else 2
        if len(accounts) <= owner:
            return "instruction"
        if accounts[owner].pubkey != wallet or accounts[delegate].pubkey not in owned:
            return "destination"
        return None
    if discriminator == TOKEN_SET_AUTHORITY:
        if len(data) < 3 or len(accounts) < 2:
            return "instruction"
        if accounts[-1].pubkey != wallet:
            return "owner"
        if data[2] == 0:
            return "destination"
        if len(data) < 35:
            return "instruction"
        new_authority = encode_address(data[3:35])
        if new_authority != wallet:
            return "destination"
        return None
    if discriminator == TOKEN_CLOSE:
        if len(accounts) < 3:
            return "instruction"
        if accounts[2].pubkey != wallet:
            return "owner"
        if accounts[1].pubkey != wallet:
            return "destination"
        return None
    return "instruction"


def _route_terms(
    instructions: tuple[DecodedInstruction, ...],
    quoted_in: int,
    quoted_out: int,
    quoted_minimum: int,
    limits: RiskLimits,
) -> RouteTerms | str | None:
    """Refuse a Jupiter route whose min-out or slippage is outside the cap.

    A transaction with no Jupiter instruction is left to the simulation.
    One that names Jupiter must decode. Minimum-out ``0`` is refused
    even when the slippage field would also fail. The instruction's
    quoted out must be the venue quote's out amount.
    """
    decoded = _one_route(instructions)
    if decoded is None:
        return None
    if isinstance(decoded, str):
        return decoded
    if decoded.min_out <= 0:
        return "min_out"
    if decoded.in_amount != quoted_in:
        return "amount_mismatch"
    if decoded.slippage_bps > limits.max_slippage_bps:
        return "slippage"
    if decoded.quoted_out != quoted_out:
        return "amount_mismatch"
    floor = decoded.quoted_out - decoded.quoted_out * limits.max_slippage_bps // 10_000
    if decoded.min_out < floor or decoded.min_out < quoted_minimum:
        return "min_out"
    return decoded


def simulated_min_out(
    instructions: tuple[DecodedInstruction, ...],
    *,
    quote_out: int,
    gained: int,
    limits: RiskLimits,
) -> str | None:
    """Bind min-out to the simulated gain, not to the route's own numbers.

    ``quote_out`` must equal the instruction's quoted out. Min-out must
    be at least ``gained * (10_000 - cap) // 10_000``. That product is
    integer division, so a float cannot round the floor down.
    """
    decoded = _one_route(instructions)
    if decoded is None:
        return None
    if isinstance(decoded, str):
        return decoded
    if isinstance(quote_out, bool) or not isinstance(quote_out, int):
        return "amount_mismatch"
    if decoded.quoted_out != quote_out:
        return "amount_mismatch"
    if isinstance(gained, bool) or not isinstance(gained, int) or gained < 0:
        return "min_out"
    cap = limits.max_slippage_bps
    required = gained * (10_000 - cap) // 10_000
    if decoded.min_out < required:
        return "min_out"
    return None


def _one_route(
    instructions: tuple[DecodedInstruction, ...],
) -> RouteTerms | str | None:
    routes = [item for item in instructions if item.program_id == JUPITER_V6_PROGRAM]
    if not routes:
        return None
    if len(routes) != 1:
        return "route_undecoded"
    decoded = decode_jupiter_route(routes[0].data)
    if isinstance(decoded, str):
        return decoded
    if decoded is None:
        return "route_undecoded"
    return decoded


def _decoded_transfer_amount(instructions: tuple[DecodedInstruction, ...]) -> int | None:
    """Sum outer token transfers. ``None`` when the swap is not that shape.

    A Jupiter instruction carries the amount in a layout this package does
    not decode. Those transactions rely on the simulation delta.
    """
    if any(item.program_id == JUPITER_V6_PROGRAM for item in instructions):
        return None
    total = 0
    found = False
    for instruction in instructions:
        if instruction.program_id not in TOKEN_PROGRAMS or not instruction.data:
            continue
        kind = instruction.data[0]
        if kind not in (TOKEN_TRANSFER, TOKEN_TRANSFER_CHECKED):
            continue
        width = 9 if kind == TOKEN_TRANSFER else 10
        if len(instruction.data) < width:
            return None
        amount = int.from_bytes(instruction.data[1:9], "little")
        total += amount
        found = True
    if not found:
        return None
    return total


def _compute(data: bytes) -> tuple[str, int] | None:
    if not data:
        return None
    if data[0] == CU_LIMIT and len(data) == 5:
        return "limit", struct.unpack_from("<I", data, 1)[0]
    if data[0] == CU_PRICE and len(data) == 9:
        return "price", struct.unpack_from("<Q", data, 1)[0]
    return None


def _cu_cap(limit: int | None, price: int | None, limits: RiskLimits) -> str | None:
    if limit is not None and limit > limits.max_cu_limit:
        return "cu_cap"
    if price is not None and price > limits.max_cu_price_micro_lamports:
        return "cu_cap"
    if price is None or price == 0:
        return None
    assumed = limits.max_cu_limit if limit is None else limit
    lamports = (price * assumed + 999_999) // 1_000_000
    if lamports > limits.max_priority_fee_lamports:
        return "cu_cap"
    return None


def _simulated(
    simulation: Simulation,
    wallet: str,
    quoted_in: int,
    quoted_out: int,
    quoted_minimum: int,
    input_account: str,
    output_account: str,
    input_mint: str,
    output_mint: str,
    limits: RiskLimits,
    instructions: tuple[DecodedInstruction, ...],
) -> GuardDecision:
    if simulation.err is not None:
        return GuardDecision(False, "simulation_failed")
    if simulation.unparsed:
        return GuardDecision(False, "amount_unverified")
    if input_mint == "" or output_mint == "" or input_mint == output_mint:
        return GuardDecision(False, "mint_mismatch")
    if simulation.owner_changed or simulation.controls_changed:
        return GuardDecision(False, "authority_change")
    if _created_authority(simulation, wallet, input_account, output_account):
        return GuardDecision(False, "authority_change")
    watched = (input_account, output_account)
    if any(account in watched for account in simulation.controlled):
        return GuardDecision(False, "authority_change")
    before = {item.account: item for item in simulation.authorities_before}
    after = {item.account: item for item in simulation.authorities_after}
    if set(before) != set(after):
        return GuardDecision(False, "authority_change")
    for account, left in before.items():
        right = after[account]
        mint_changed = left.mint_authority != right.mint_authority
        freeze_changed = left.freeze_authority != right.freeze_authority
        if mint_changed or freeze_changed:
            return GuardDecision(False, "authority_change")
    if any(account != wallet for account in simulation.closed):
        return GuardDecision(False, "account_closed")
    deltas = {item.account: item for item in simulation.deltas}
    spent = deltas.get(input_account)
    received = deltas.get(output_account)
    if spent is None or received is None:
        return GuardDecision(False, "amount_unverified")
    if spent.owner != wallet or received.owner != wallet:
        return GuardDecision(False, "owner")
    if spent.mint != input_mint or received.mint != output_mint:
        return GuardDecision(False, "mint_mismatch")
    for item in simulation.deltas:
        if item.owner != wallet or item.account in (input_account, output_account):
            continue
        if item.before > item.after:
            return GuardDecision(False, "amount_mismatch")
    if spent.before - spent.after != quoted_in:
        return GuardDecision(False, "amount_mismatch")
    gained = received.after - received.before
    if gained < quoted_minimum:
        return GuardDecision(False, "amount_mismatch")
    bound = simulated_min_out(
        instructions,
        quote_out=quoted_out,
        gained=gained,
        limits=limits,
    )
    if bound is not None:
        return GuardDecision(False, bound)
    lamports_before = dict(simulation.lamports_before)
    lamports_after = dict(simulation.lamports_after)
    if wallet not in lamports_before or wallet not in lamports_after:
        return GuardDecision(False, "per_tx_fee")
    if lamports_after[wallet] <= 0:
        return GuardDecision(False, "per_tx_fee")
    drop = lamports_before[wallet] - lamports_after[wallet]
    if drop < 0 or drop > limits.max_fee_lamports:
        return GuardDecision(False, "per_tx_fee")
    rent = _rent(simulation, wallet)
    if rent is None:
        return GuardDecision(False, "per_tx_fee")
    fees = split_lamport_drop(drop, rent=rent, signatures=1)
    if fees is None or fees.total_lamports() != drop:
        return GuardDecision(False, "per_tx_fee")
    return GuardDecision(True, "ok", fees)


def _rent(simulation: Simulation, wallet: str) -> int | None:
    """Lamports other accounts gained. Those are rent, not the fee."""
    before = dict(simulation.lamports_before)
    after = dict(simulation.lamports_after)
    gained = 0
    for account, end in after.items():
        if account == wallet:
            continue
        start = before.get(account, 0)
        if end < 0 or start < 0:
            return None
        if end > start:
            gained += end - start
    return gained


def _created_authority(
    simulation: Simulation,
    wallet: str,
    input_account: str,
    output_account: str,
) -> bool:
    """A token account born in this transaction must not grant a delegate.

    Wallet-owned accounts, and the input and output accounts, are checked.
    An account owned by someone else is not this wallet's authority.
    """
    for account, owner, delegate, close_authority in simulation.created:
        watched = owner == wallet or account in (input_account, output_account)
        if watched and (delegate != b"" or close_authority != b""):
            return True
    return False


def _index(keys: tuple[str, ...], program_id: str) -> int | None:
    try:
        return keys.index(program_id)
    except ValueError:
        return None


def encode_address(raw: bytes) -> str:
    from app.venue.address import encode

    return encode(raw)


def system_transfer_data(lamports: int) -> bytes:
    return struct.pack("<IQ", SYSTEM_TRANSFER, lamports)


def advance_nonce_data() -> bytes:
    return struct.pack("<I", SYSTEM_ADVANCE_NONCE)


def token_transfer_data(amount: int) -> bytes:
    return bytes([TOKEN_TRANSFER]) + amount.to_bytes(8, "little")


def compute_limit_data(limit: int) -> bytes:
    return bytes([CU_LIMIT]) + struct.pack("<I", limit)


def compute_price_data(micro_lamports: int) -> bytes:
    return bytes([CU_PRICE]) + struct.pack("<Q", micro_lamports)


def address_of(text: str) -> bytes:
    return address_bytes(text)
