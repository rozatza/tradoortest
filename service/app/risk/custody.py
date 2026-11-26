"""Seed file for the internal auto signer.

The operator creates the seed outside the repo. This module reads the
file named by ``LIVE_SIGNER_KEY_FILE`` only when ``LIVE_SIGNER`` is
``local`` and ``LIVE_TRADING_ENABLED`` is exactly ``1``. The file is 32
raw bytes. The derived public key must match ``LIVE_WALLET_PUBKEY``.

The holder redacts itself. Failures raise a code and do not carry the
seed. Signing is armed only for the message the guard stored.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import secrets
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.risk.wallet import configured_wallet
from app.venue.address import encode
from app.venue.flags import live_trading_enabled

_SEED_LEN = 32
_log = logging.getLogger("app.risk.custody")
_Lookup = Callable[[int], tuple[bytes, str] | None]
_lookup: _Lookup | None = None
_armed: contextvars.ContextVar[bytes | None] = contextvars.ContextVar(
    "tradoor_guarded_message",
    default=None,
)


class SignerClosed(Exception):
    """The signer refused. ``code`` is the only text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"SignerClosed({self.code})"

    def __str__(self) -> str:
        return self.code


class RedactedSigner:
    """Ed25519 holder. ``repr`` and ``str`` do not include the seed."""

    __slots__ = ("_material",)

    def __init__(self, material: Ed25519PrivateKey) -> None:
        self._material = material

    def __repr__(self) -> str:
        return "RedactedSigner(redacted)"

    def __str__(self) -> str:
        return "RedactedSigner(redacted)"

    def __format__(self, spec: str) -> str:
        return "RedactedSigner(redacted)"

    def __bytes__(self) -> bytes:
        raise SignerClosed("signer_redacted")

    def __reduce__(self) -> str:
        raise SignerClosed("signer_redacted")

    def __getstate__(self) -> None:
        raise SignerClosed("signer_redacted")

    def public_bytes(self) -> bytes:
        return self._material.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def _sign(self, message: bytes) -> bytes:
        armed = _armed.get()
        if armed is None or message is not armed or not message:
            raise SignerClosed("unguarded")
        return self._material.sign(armed)


def bind_guarded_lookup(lookup: _Lookup) -> None:
    """Register the book read. ``signer`` calls this once, at import."""
    global _lookup
    _lookup = lookup


def sign_guarded(held: RedactedSigner, pending_id: int, message: bytes) -> bytes:
    """Sign the stored message when ``message`` matches it byte for byte.

    Altered bytes, an unknown id, and a call with no armed book are
    refused before the holder is asked to sign.
    """
    lookup = _lookup
    if lookup is None or not isinstance(held, RedactedSigner):
        raise SignerClosed("unguarded")
    if isinstance(pending_id, bool) or not isinstance(pending_id, int):
        raise SignerClosed("unguarded")
    if not isinstance(message, bytes):
        raise SignerClosed("unguarded")
    found = lookup(pending_id)
    if found is None:
        raise SignerClosed("unguarded")
    stored, stored_hash = found
    if not isinstance(stored, bytes) or not isinstance(stored_hash, str) or not stored:
        raise SignerClosed("unguarded")
    digest = hashlib.sha256(message).hexdigest()
    if not secrets.compare_digest(message, stored):
        raise SignerClosed("unguarded")
    if not secrets.compare_digest(digest, stored_hash):
        raise SignerClosed("unguarded")
    token = _armed.set(stored)
    try:
        return held._sign(stored)
    finally:
        _armed.reset(token)


def load_signer() -> RedactedSigner:
    """Read the seed file, or refuse with a code and no seed bytes."""
    if os.environ.get("LIVE_SIGNER") != "local" or not live_trading_enabled():
        raise _closed("signer_disabled")
    raw_path = os.environ.get("LIVE_SIGNER_KEY_FILE")
    if raw_path is None or raw_path.strip() == "":
        raise _closed("signer_unconfigured")
    expected = configured_wallet()
    if expected is None:
        raise _closed("wallet_unconfigured")
    path = Path(raw_path.strip())
    try:
        info = path.stat()
    except OSError:
        raise _closed("signer_key_unreadable") from None
    if not path.is_file():
        raise _closed("signer_key_unreadable") from None
    if info.st_size != _SEED_LEN:
        raise _closed("signer_key_length") from None
    try:
        blob = path.read_bytes()
    except OSError:
        raise _closed("signer_key_unreadable") from None
    buf = bytearray(blob)
    del blob
    try:
        if len(buf) != _SEED_LEN:
            raise _closed("signer_key_length") from None
        try:
            material = Ed25519PrivateKey.from_private_bytes(bytes(buf))
        except Exception:
            raise _closed("signer_key_malformed") from None
        held = RedactedSigner(material)
        derived = encode(held.public_bytes())
        if len(derived) != len(expected) or not secrets.compare_digest(derived, expected):
            raise _closed("signer_pubkey_mismatch") from None
        return held
    finally:
        buf[:] = b"\x00" * len(buf)


def _closed(code: str) -> SignerClosed:
    _log.warning("signer closed code=%s", code)
    return SignerClosed(code)
