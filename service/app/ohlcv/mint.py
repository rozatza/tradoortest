"""Solana mint checks that run before any provider request.

A mint is the base58 encoding of a 32-byte address. Anything else is
rejected here. This does not ask an RPC whether the account is an SPL
mint: a well-formed address the provider does not know is a later
``no_pool`` (or ``no_dex_pool_yet``) error, not this one.
"""

from __future__ import annotations

from app.ohlcv.errors import InvalidMint

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: index for index, char in enumerate(_ALPHABET)}
# 32 bytes is 44 characters at the most, and 32 characters at the least
# (a value whose high bytes are zero is shorter). Longer input is rejected
# without being copied into the error text.
_MAX_TEXT = 44


def require_mint(value: object) -> str:
    """Return the mint, or raise ``InvalidMint`` before any network call."""
    if not isinstance(value, str):
        raise InvalidMint("invalid mint")
    text = value.strip()
    if text == "" or len(text) > _MAX_TEXT or any(char not in _INDEX for char in text):
        raise InvalidMint("invalid mint")
    decoded = _decode(text)
    if len(decoded) != 32:
        raise InvalidMint("invalid mint")
    return text


def _decode(text: str) -> bytes:
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
