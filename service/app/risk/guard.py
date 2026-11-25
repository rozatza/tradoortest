"""Pre-sign checks on an unsigned version-0 transaction.

The quote's in, out, and minimum amounts are not read off the
transaction. Those fields are copies of the quote. Outer token
transfers are decoded. Every transaction is also simulated, and the
balance delta plus the mint and freeze authorities are checked. This
module does not sign and does not submit.
"""

from __future__ import annotations

import struct

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
from app.risk.rpc import ChainRpc, Simulation
from app.venue.address import address_bytes
from app.venue.errors import VenueError
from app.venue.tx import decode_unsigned_v0
from app.venue.types import AccountRole, DecodedInstruction, UnsignedTransaction


class GuardDecision:
    """A rejected transaction is not resized and not signed."""

    def __init__(self, accepted: bool, code: str) -> None:
        self.accepted = accepted
        self.code = code

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
    limits: RiskLimits | None = None,
) -> GuardDecision:
    """Check ``tx.transaction``. Quote-copied amount fields are ignored."""
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
        chosen,
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
        dest = 1
        authority = 3 if discriminator == TOKEN_TRANSFER_CHECKED else 2
        if len(accounts) <= authority:
            return "instruction"
        if accounts[authority].pubkey != wallet:
            return "owner"
        if accounts[dest].pubkey not in owned:
            return "destination"
        return None
    if discriminator in (TOKEN_APPROVE, TOKEN_APPROVE_CHECKED):
        delegate = 1
        owner = 3 if discriminator == TOKEN_APPROVE_CHECKED else 2
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
    limits: RiskLimits,
) -> GuardDecision:
    del quoted_out
    if simulation.err is not None:
        return GuardDecision(False, "simulation_failed")
    if simulation.owner_changed:
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
    deltas = {item.account: item for item in simulation.deltas}
    spent = deltas.get(input_account)
    received = deltas.get(output_account)
    if spent is None or received is None:
        return GuardDecision(False, "amount_unverified")
    if spent.owner != wallet or received.owner != wallet:
        return GuardDecision(False, "owner")
    if spent.before - spent.after != quoted_in:
        return GuardDecision(False, "amount_mismatch")
    gained = received.after - received.before
    if gained < quoted_minimum:
        return GuardDecision(False, "amount_mismatch")
    lamports_before = dict(simulation.lamports_before)
    lamports_after = dict(simulation.lamports_after)
    if wallet in lamports_before and wallet in lamports_after:
        drop = lamports_before[wallet] - lamports_after[wallet]
        if drop > limits.max_fee_lamports:
            return GuardDecision(False, "per_tx_fee")
    return GuardDecision(True, "ok")


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
