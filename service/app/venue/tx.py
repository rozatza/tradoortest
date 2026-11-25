"""Unsigned version-0 transactions.

The message layout is Solana's v0 message: a ``0x80`` prefix, the legacy
header, static keys, the blockhash, instructions, then address-table
lookups. Lookup indexes are resolved with the table contents the venue
returned, so the decoded instruction lists pubkeys rather than indexes.

Program ids stay in the static keys. Other non-signer accounts that the
table contains are loaded from it. Signers stay static. The fee payer is
the first static key and is a writable signer.

The signature block is all zeros. A non-zero signature is rejected.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.venue.address import address_bytes, encode, require_address
from app.venue.errors import VenueRejected, VenueSchemaError
from app.venue.types import (
    AccountRole,
    DecodedInstruction,
    ResolvedLookupTable,
    UnsignedTransaction,
    VenueQuote,
)

_MAX_ACCOUNTS = 64
_MAX_INSTRUCTIONS = 32
_MAX_TABLES = 8
_MAX_TABLE_ADDRESSES = 256
_MAX_DATA = 1232
_PACKET = 1232
_SIG_LEN = 64


@dataclass(frozen=True, slots=True)
class _Meta:
    pubkey: bytes
    signer: bool
    writable: bool
    invoked: bool
    drained: bool


def assemble_unsigned(
    *,
    payer: str,
    instructions: Sequence[DecodedInstruction],
    lookup_tables: Sequence[tuple[str, tuple[str, ...]]],
    recent_blockhash: bytes,
    quote: VenueQuote,
    last_valid_block_height: int,
) -> UnsignedTransaction:
    """Compile, decode, and keep the transaction only when the two agree."""
    payer = require_address(payer)
    if (
        isinstance(last_valid_block_height, bool)
        or not isinstance(last_valid_block_height, int)
        or last_valid_block_height < 0
    ):
        raise VenueSchemaError("bad_block_height")
    drafts = tuple(instructions)
    if not drafts or len(drafts) > _MAX_INSTRUCTIONS:
        raise VenueSchemaError("bad_instructions")
    _reject_extra_signers(drafts, payer)
    if quote.input_mint == quote.output_mint:
        raise VenueRejected("mint_mismatch")
    blob, tables = _compile(payer, drafts, lookup_tables, recent_blockhash)
    if len(blob) > _PACKET:
        raise VenueRejected("transaction_too_large")
    decoded = decode_unsigned_v0(blob, tables)
    _require_round_trip(drafts, decoded, payer)
    keys = decoded[0]
    if quote.input_mint not in keys or quote.output_mint not in keys:
        raise VenueRejected("mint_mismatch")
    fee_payer = decoded[1]
    if fee_payer != payer:
        raise VenueRejected("fee_payer")
    return UnsignedTransaction(
        transaction=blob,
        instructions=decoded[2],
        fee_payer=fee_payer,
        taker=payer,
        account_keys=keys,
        static_account_count=decoded[3],
        lookup_tables=decoded[4],
        input_mint=quote.input_mint,
        output_mint=quote.output_mint,
        quote_in_amount=quote.in_amount,
        quote_out_amount=quote.out_amount,
        quote_minimum_out_amount=quote.minimum_out_amount,
        last_valid_block_height=last_valid_block_height,
        transaction_version=0,
    )


def decode_unsigned_v0(
    blob: bytes,
    lookup_tables: Sequence[tuple[str, tuple[str, ...]]],
) -> tuple[
    tuple[str, ...],
    str,
    tuple[DecodedInstruction, ...],
    int,
    tuple[ResolvedLookupTable, ...],
]:
    """Resolve lookup indexes and return the guard's view of ``blob``.

    The tuple is account keys, fee payer, instructions, static key count,
    and the tables the message actually referenced.
    """
    if not isinstance(blob, bytes | bytearray) or not blob:
        raise VenueSchemaError("truncated_transaction")
    data = bytes(blob)
    pos = 0
    sig_count, pos = _read_shortvec(data, pos)
    if sig_count < 1 or sig_count > _MAX_ACCOUNTS:
        raise VenueSchemaError("bad_signatures")
    sig_end = pos + sig_count * _SIG_LEN
    if sig_end > len(data):
        raise VenueSchemaError("truncated_transaction")
    if any(byte != 0 for byte in data[pos:sig_end]):
        raise VenueRejected("signed_transaction")
    pos = sig_end
    if pos >= len(data):
        raise VenueSchemaError("truncated_transaction")
    prefix = data[pos]
    pos += 1
    if prefix & 0x80 == 0 or (prefix & 0x7F) != 0:
        raise VenueSchemaError("unsupported_version")
    if pos + 3 > len(data):
        raise VenueSchemaError("truncated_transaction")
    num_required = data[pos]
    num_readonly_signed = data[pos + 1]
    num_readonly_unsigned = data[pos + 2]
    pos += 3
    if num_required != sig_count or num_required < 1:
        raise VenueSchemaError("bad_signatures")
    if num_readonly_signed > num_required:
        raise VenueSchemaError("bad_header")
    static_len, pos = _read_shortvec(data, pos)
    if static_len < num_required or static_len > _MAX_ACCOUNTS:
        raise VenueSchemaError("bad_accounts")
    if num_readonly_unsigned > static_len - num_required:
        raise VenueSchemaError("bad_header")
    static_end = pos + static_len * 32
    if static_end + 32 > len(data):
        raise VenueSchemaError("truncated_transaction")
    static = [data[pos + index * 32 : pos + (index + 1) * 32] for index in range(static_len)]
    pos = static_end
    blockhash = data[pos : pos + 32]
    if len(blockhash) != 32:
        raise VenueSchemaError("truncated_transaction")
    pos += 32
    del blockhash  # carried in the bytes; the guard reads accounts, not the hash
    ix_count, pos = _read_shortvec(data, pos)
    if ix_count < 1 or ix_count > _MAX_INSTRUCTIONS:
        raise VenueSchemaError("bad_instructions")
    raw_ixs: list[tuple[int, list[int], bytes]] = []
    for _ in range(ix_count):
        if pos >= len(data):
            raise VenueSchemaError("truncated_transaction")
        program_index = data[pos]
        pos += 1
        account_count, pos = _read_shortvec(data, pos)
        if account_count > _MAX_ACCOUNTS or pos + account_count > len(data):
            raise VenueSchemaError("bad_accounts")
        accounts = list(data[pos : pos + account_count])
        pos += account_count
        data_len, pos = _read_shortvec(data, pos)
        if data_len > _MAX_DATA or pos + data_len > len(data):
            raise VenueSchemaError("bad_instruction_data")
        raw_ixs.append((program_index, accounts, data[pos : pos + data_len]))
        pos += data_len
    lookup_count, pos = _read_shortvec(data, pos)
    if lookup_count > _MAX_TABLES:
        raise VenueSchemaError("bad_lookup_table")
    table_map = {address: addresses for address, addresses in lookup_tables}
    if len(table_map) != len(lookup_tables):
        raise VenueSchemaError("bad_lookup_table")
    loaded_writable: list[bytes] = []
    loaded_readonly: list[bytes] = []
    used: list[ResolvedLookupTable] = []
    for _ in range(lookup_count):
        if pos + 32 > len(data):
            raise VenueSchemaError("truncated_transaction")
        table_key = encode(data[pos : pos + 32])
        pos += 32
        writable_len, pos = _read_shortvec(data, pos)
        if writable_len > _MAX_TABLE_ADDRESSES or pos + writable_len > len(data):
            raise VenueSchemaError("bad_lookup_table")
        writable_indexes = list(data[pos : pos + writable_len])
        pos += writable_len
        readonly_len, pos = _read_shortvec(data, pos)
        if readonly_len > _MAX_TABLE_ADDRESSES or pos + readonly_len > len(data):
            raise VenueSchemaError("bad_lookup_table")
        readonly_indexes = list(data[pos : pos + readonly_len])
        pos += readonly_len
        addresses = table_map.get(table_key)
        if addresses is None:
            raise VenueSchemaError("unresolved_lookup_table")
        loaded_writable.extend(_pick(addresses, writable_indexes))
        loaded_readonly.extend(_pick(addresses, readonly_indexes))
        used.append(
            ResolvedLookupTable(
                address=table_key,
                addresses=tuple(addresses),
                writable_indexes=tuple(writable_indexes),
                readonly_indexes=tuple(readonly_indexes),
            )
        )
    if pos != len(data):
        raise VenueSchemaError("trailing_bytes")
    full = static + loaded_writable + loaded_readonly
    if len(full) > 256:
        raise VenueSchemaError("bad_accounts")
    keys = tuple(encode(item) for item in full)
    if len(set(keys)) != len(keys):
        raise VenueSchemaError("bad_accounts")
    # Writable signers occupy the front. A readonly account 0 cannot pay.
    if num_readonly_signed == num_required:
        raise VenueRejected("fee_payer")
    fee_payer = keys[0]
    instructions: list[DecodedInstruction] = []
    for program_index, account_indexes, ix_data in raw_ixs:
        if program_index >= len(keys):
            raise VenueSchemaError("bad_accounts")
        roles: list[AccountRole] = []
        for index in account_indexes:
            if index >= len(keys):
                raise VenueSchemaError("bad_accounts")
            roles.append(
                AccountRole(
                    pubkey=keys[index],
                    signer=_is_signer(index, num_required),
                    writable=_is_writable(
                        index,
                        num_required,
                        num_readonly_signed,
                        num_readonly_unsigned,
                        static_len,
                        len(loaded_writable),
                    ),
                )
            )
        instructions.append(
            DecodedInstruction(
                program_id=keys[program_index],
                accounts=tuple(roles),
                data=ix_data,
            )
        )
    return keys, fee_payer, tuple(instructions), static_len, tuple(used)


def _compile(
    payer: str,
    instructions: Sequence[DecodedInstruction],
    lookup_tables: Sequence[tuple[str, tuple[str, ...]]],
    recent_blockhash: bytes,
) -> tuple[bytes, tuple[tuple[str, tuple[str, ...]], ...]]:
    if not isinstance(recent_blockhash, bytes) or len(recent_blockhash) != 32:
        raise VenueSchemaError("bad_blockhash")
    if len(lookup_tables) > _MAX_TABLES:
        raise VenueSchemaError("bad_lookup_table")
    payer_bytes = address_bytes(payer)
    metas: dict[bytes, _Meta] = {}

    def slot(pubkey: bytes, *, signer: bool, writable: bool, invoked: bool) -> None:
        found = metas.get(pubkey)
        if found is None:
            metas[pubkey] = _Meta(pubkey, signer, writable, invoked, False)
            return
        metas[pubkey] = _Meta(
            pubkey,
            found.signer or signer,
            found.writable or writable,
            found.invoked or invoked,
            found.drained,
        )

    slot(payer_bytes, signer=True, writable=True, invoked=False)
    for instruction in instructions:
        program = address_bytes(instruction.program_id)
        slot(program, signer=False, writable=False, invoked=True)
        if len(instruction.accounts) > _MAX_ACCOUNTS:
            raise VenueSchemaError("bad_accounts")
        if len(instruction.data) > _MAX_DATA:
            raise VenueSchemaError("bad_instruction_data")
        for account in instruction.accounts:
            slot(
                address_bytes(account.pubkey),
                signer=account.signer,
                writable=account.writable,
                invoked=False,
            )
    tables: list[tuple[bytes, tuple[bytes, ...], str, tuple[str, ...]]] = []
    seen_tables: set[bytes] = set()
    for address, contents in lookup_tables:
        table_bytes = address_bytes(address)
        if table_bytes in seen_tables:
            raise VenueSchemaError("bad_lookup_table")
        seen_tables.add(table_bytes)
        if len(contents) > _MAX_TABLE_ADDRESSES:
            raise VenueSchemaError("bad_lookup_table")
        encoded = tuple(require_address(item) for item in contents)
        if len(set(encoded)) != len(encoded):
            raise VenueSchemaError("bad_lookup_table")
        tables.append(
            (table_bytes, tuple(address_bytes(item) for item in encoded), address, encoded)
        )

    loaded_w: list[bytes] = []
    loaded_r: list[bytes] = []
    lookups: list[tuple[bytes, list[int], list[int], str, tuple[str, ...]]] = []
    for table_bytes, contents, address, encoded in tables:
        w_idx, w_keys = _drain(metas, contents, writable=True)
        r_idx, r_keys = _drain(metas, contents, writable=False)
        if not w_idx and not r_idx:
            continue
        loaded_w.extend(w_keys)
        loaded_r.extend(r_keys)
        lookups.append((table_bytes, w_idx, r_idx, address, encoded))

    writable_signers: list[bytes] = []
    readonly_signers: list[bytes] = []
    writable_non: list[bytes] = []
    readonly_non: list[bytes] = []
    for meta in metas.values():
        if meta.drained:
            continue
        if meta.signer and meta.writable:
            writable_signers.append(meta.pubkey)
        elif meta.signer:
            readonly_signers.append(meta.pubkey)
        elif meta.writable:
            writable_non.append(meta.pubkey)
        else:
            readonly_non.append(meta.pubkey)
    static = writable_signers + readonly_signers + writable_non + readonly_non
    full = static + loaded_w + loaded_r
    if len(full) > 256 or len(set(full)) != len(full):
        raise VenueSchemaError("bad_accounts")
    index_of = {key: index for index, key in enumerate(full)}
    header = bytes(
        (
            len(writable_signers) + len(readonly_signers),
            len(readonly_signers),
            len(readonly_non),
        )
    )
    message = bytearray(b"\x80")
    message.extend(header)
    message.extend(_shortvec(len(static)))
    for key in static:
        message.extend(key)
    message.extend(recent_blockhash)
    message.extend(_shortvec(len(instructions)))
    for instruction in instructions:
        program = address_bytes(instruction.program_id)
        message.append(index_of[program])
        message.extend(_shortvec(len(instruction.accounts)))
        for account in instruction.accounts:
            message.append(index_of[address_bytes(account.pubkey)])
        message.extend(_shortvec(len(instruction.data)))
        message.extend(instruction.data)
    message.extend(_shortvec(len(lookups)))
    for table_bytes, w_idx, r_idx, _address, _encoded in lookups:
        message.extend(table_bytes)
        message.extend(_shortvec(len(w_idx)))
        message.extend(bytes(w_idx))
        message.extend(_shortvec(len(r_idx)))
        message.extend(bytes(r_idx))
    sig_count = header[0]
    blob = bytearray(_shortvec(sig_count))
    blob.extend(b"\x00" * _SIG_LEN * sig_count)
    blob.extend(message)
    resolved = tuple((address, encoded) for _b, _w, _r, address, encoded in lookups)
    # Pass every supplied table to the decoder so an index can be checked
    # against the venue's list, including a table the message references.
    supplied = tuple((address, encoded) for _b, _c, address, encoded in tables)
    # ``resolved`` is the subset the message uses. The decoder needs the
    # contents of those tables. ``supplied`` covers them.
    del resolved
    return bytes(blob), supplied


def _drain(
    metas: dict[bytes, _Meta], contents: tuple[bytes, ...], *, writable: bool
) -> tuple[list[int], list[bytes]]:
    indexes: list[int] = []
    keys: list[bytes] = []
    positions = {key: index for index, key in enumerate(contents)}
    for pubkey, meta in list(metas.items()):
        if meta.drained or meta.signer or meta.invoked or meta.writable != writable:
            continue
        index = positions.get(pubkey)
        if index is None:
            continue
        if index > 255:
            raise VenueSchemaError("bad_lookup_table")
        metas[pubkey] = _Meta(meta.pubkey, meta.signer, meta.writable, meta.invoked, True)
        indexes.append(index)
        keys.append(pubkey)
    return indexes, keys


def _reject_extra_signers(instructions: Sequence[DecodedInstruction], payer: str) -> None:
    for instruction in instructions:
        for account in instruction.accounts:
            if account.signer and account.pubkey != payer:
                raise VenueRejected("extra_signer")


def _require_round_trip(
    drafts: Sequence[DecodedInstruction],
    decoded: tuple[
        tuple[str, ...],
        str,
        tuple[DecodedInstruction, ...],
        int,
        tuple[ResolvedLookupTable, ...],
    ],
    payer: str,
) -> None:
    found = decoded[2]
    if len(found) != len(drafts):
        raise VenueSchemaError("transaction_mismatch")
    merged = _merged_roles(drafts, payer)
    for draft, item in zip(drafts, found, strict=True):
        if item.program_id != draft.program_id or item.data != draft.data:
            raise VenueSchemaError("transaction_mismatch")
        if len(item.accounts) != len(draft.accounts):
            raise VenueSchemaError("transaction_mismatch")
        for account, role in zip(draft.accounts, item.accounts, strict=True):
            signer, writable = merged[account.pubkey]
            if role.pubkey != account.pubkey or role.signer != signer or role.writable != writable:
                raise VenueSchemaError("transaction_mismatch")


def _merged_roles(
    drafts: Sequence[DecodedInstruction], payer: str
) -> dict[str, tuple[bool, bool]]:
    roles: dict[str, tuple[bool, bool]] = {payer: (True, True)}
    for draft in drafts:
        for account in draft.accounts:
            signer, writable = roles.get(account.pubkey, (False, False))
            roles[account.pubkey] = (signer or account.signer, writable or account.writable)
    roles[payer] = (True, True)
    return roles


def _pick(addresses: tuple[str, ...], indexes: list[int]) -> list[bytes]:
    loaded: list[bytes] = []
    for index in indexes:
        if index >= len(addresses):
            raise VenueSchemaError("unresolved_lookup_table")
        loaded.append(address_bytes(addresses[index]))
    return loaded


def _is_signer(index: int, num_required: int) -> bool:
    return index < num_required


def _is_writable(
    index: int,
    num_required: int,
    num_readonly_signed: int,
    num_readonly_unsigned: int,
    static_len: int,
    loaded_writable: int,
) -> bool:
    if index < num_required:
        return index < num_required - num_readonly_signed
    if index < static_len:
        readonly_start = static_len - num_readonly_unsigned
        return index < readonly_start
    loaded_index = index - static_len
    return loaded_index < loaded_writable


def _shortvec(value: int) -> bytes:
    if value < 0 or value > 0xFFFF:
        raise VenueSchemaError("bad_compact")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value == 0:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _read_shortvec(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for taken in range(3):
        if pos >= len(data):
            raise VenueSchemaError("truncated_transaction")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            if taken > 0 and byte == 0:
                raise VenueSchemaError("bad_compact")
            if value > 0xFFFF:
                raise VenueSchemaError("bad_compact")
            return value, pos
        shift += 7
    raise VenueSchemaError("bad_compact")
