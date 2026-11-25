"""Solana address codec.

A public key is 32 bytes, written as base58. The same check the mint
helper uses. This copy stays in the venue so a bad address raises
``VenueError`` and does not pull a provider error into the message.
"""

from __future__ import annotations

from app.venue.errors import VenueError

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: index for index, char in enumerate(_ALPHABET)}
_MAX_TEXT = 44


def require_address(value: object) -> str:
    """Return the address, or raise before any network call."""
    if not isinstance(value, str) or not _is_address(value):
        raise VenueError("invalid_address")
    return value


def encode(data: bytes) -> str:
    """Base58. Leading zero bytes become leading ``1`` characters."""
    if not data:
        return ""
    zeros = 0
    for byte in data:
        if byte != 0:
            break
        zeros += 1
    value = int.from_bytes(data, "big")
    chars: list[str] = []
    while value:
        value, rem = divmod(value, 58)
        chars.append(_ALPHABET[rem])
    return ("1" * zeros) + "".join(reversed(chars))


def decode(text: str) -> bytes:
    """Decode a 32-byte address. The caller has already checked the alphabet."""
    value = 0
    for char in text:
        value = value * 58 + _INDEX[char]
    pad = 0
    for char in text:
        if char != "1":
            break
        pad += 1
    if value == 0:
        body = b""
    else:
        width = (value.bit_length() + 7) // 8
        body = value.to_bytes(width, "big")
    return b"\x00" * pad + body


def address_bytes(text: str) -> bytes:
    """32 bytes for an address that ``require_address`` would accept."""
    if not _is_address(text):
        raise VenueError("invalid_address")
    return decode(text)


def _is_address(text: str) -> bool:
    if text == "" or len(text) > _MAX_TEXT or any(char not in _INDEX for char in text):
        return False
    return len(decode(text)) == 32
