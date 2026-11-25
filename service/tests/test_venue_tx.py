"""Version-0 codec. Lookup indexes become pubkeys. Signatures stay zero."""

from __future__ import annotations

import pytest

from app.venue.address import encode
from app.venue.errors import VenueRejected, VenueSchemaError
from app.venue.tx import _compile, decode_unsigned_v0
from app.venue.types import AccountRole, DecodedInstruction

_PAYER = encode(bytes([1]) * 32)
_PROGRAM = encode(bytes([3]) * 32)
_ACCOUNT = encode(bytes([4]) * 32)
_TABLE = encode(bytes([5]) * 32)


def test_unsigned_v0_bytes_match_the_message_layout() -> None:
    """One static program, no lookup table. The bytes are the v0 layout."""
    blob, _tables = _compile(
        _PAYER,
        (DecodedInstruction(_PROGRAM, (), b"\x01"),),
        (),
        bytes([2]) * 32,
    )
    message = (
        bytes([0x80, 0x01, 0x00, 0x01, 0x02])
        + bytes([1]) * 32
        + bytes([3]) * 32
        + bytes([2]) * 32
        + bytes([0x01, 0x01, 0x00, 0x01, 0x01, 0x00])
    )
    assert blob == bytes([0x01]) + bytes(64) + message
    keys, fee_payer, instructions, static_count, tables = decode_unsigned_v0(blob, ())
    assert fee_payer == _PAYER
    assert keys == (_PAYER, _PROGRAM)
    assert static_count == 2
    assert tables == ()
    assert instructions == (DecodedInstruction(_PROGRAM, (), b"\x01"),)


def test_lookup_table_account_is_resolved_and_not_left_as_an_index() -> None:
    blob, tables = _compile(
        _PAYER,
        (
            DecodedInstruction(
                _PROGRAM,
                (AccountRole(_ACCOUNT, False, True),),
                b"",
            ),
        ),
        ((_TABLE, (_ACCOUNT,)),),
        bytes([2]) * 32,
    )
    keys, fee_payer, instructions, static_count, used = decode_unsigned_v0(blob, tables)
    assert fee_payer == _PAYER
    assert static_count == 2
    assert keys[static_count] == _ACCOUNT
    assert _ACCOUNT not in keys[:static_count]
    assert instructions[0].program_id == _PROGRAM
    assert instructions[0].accounts == (AccountRole(_ACCOUNT, False, True),)
    assert used[0].address == _TABLE
    assert used[0].writable_indexes == (0,)
    assert used[0].addresses == (_ACCOUNT,)


def test_a_signature_byte_is_rejected() -> None:
    blob, tables = _compile(
        _PAYER,
        (DecodedInstruction(_PROGRAM, (), b""),),
        (),
        bytes([2]) * 32,
    )
    signed = bytearray(blob)
    signed[1] = 1
    with pytest.raises(VenueRejected, match="signed_transaction"):
        decode_unsigned_v0(bytes(signed), tables)


def test_a_lookup_index_past_the_table_is_rejected() -> None:
    blob, _tables = _compile(
        _PAYER,
        (
            DecodedInstruction(
                _PROGRAM,
                (AccountRole(_ACCOUNT, False, True),),
                b"",
            ),
        ),
        ((_TABLE, (_ACCOUNT,)),),
        bytes([2]) * 32,
    )
    with pytest.raises(VenueSchemaError, match="unresolved_lookup_table"):
        decode_unsigned_v0(blob, ((_TABLE, ()),))


def test_a_truncated_transaction_is_rejected() -> None:
    with pytest.raises(VenueSchemaError, match="truncated_transaction"):
        decode_unsigned_v0(b"\x01" + bytes(10), ())
