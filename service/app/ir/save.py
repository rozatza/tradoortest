"""Versioned strategy save document.

The file envelope is separate from the stored row. `user_id` stays on the
row (`StoredStrategy`) and is not part of this document, so the IR hash does
not change when the owner does. See `docs/save-format.md`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.ir.canonical import canonicalize, ir_hash
from app.ir.constants import IR_VERSION
from app.ir.depth import check_json_value

FORMAT = "tradoor.strategy"
FORMAT_VERSION = "1"

_SAVE_KEYS = ("format", "format_version", "ir_version", "ir_hash", "ir")


class SaveError(Exception):
    """A save document failed a version or hash check.

    `code` is `unsupported_format`, `unsupported_ir_version`,
    `ir_version_mismatch`, `ir_hash_mismatch`, or `invalid_save`.
    """

    def __init__(self, message: str, *, code: str) -> None:
        self.message = message
        self.code = code
        super().__init__(f"{code}: {message}")


def save_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap canonical IR in a versioned save document.

    The embedded `ir` is canonical. `ir_hash` is `ir_hash` of that object.
    `ir_version` is copied from the IR and must be `"1.0"`.
    """
    ir = canonicalize(document)
    version = ir.get("ir_version")
    if version != IR_VERSION:
        raise SaveError(
            f"ir_version must be {IR_VERSION!r}",
            code="unsupported_ir_version",
        )
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "ir_version": version,
        "ir_hash": ir_hash(ir),
        "ir": ir,
    }


def load_document(saved: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical IR after checking format, version, and hash."""
    check_json_value(saved)
    if not isinstance(saved, Mapping):
        raise SaveError("save document must be an object", code="invalid_save")
    if saved.get("format") != FORMAT or saved.get("format_version") != FORMAT_VERSION:
        raise SaveError(
            f"save format must be {FORMAT} version {FORMAT_VERSION}",
            code="unsupported_format",
        )
    ir = saved.get("ir")
    if not isinstance(ir, Mapping):
        raise SaveError("save document is missing ir", code="invalid_save")
    canonical = canonicalize(ir)
    version = canonical.get("ir_version")
    if version != IR_VERSION:
        raise SaveError(
            f"ir_version must be {IR_VERSION!r}",
            code="unsupported_ir_version",
        )
    if saved.get("ir_version") != version:
        raise SaveError(
            "envelope ir_version does not match ir.ir_version",
            code="ir_version_mismatch",
        )
    digest = ir_hash(canonical)
    if saved.get("ir_hash") != digest:
        raise SaveError("ir_hash does not match the canonical IR", code="ir_hash_mismatch")
    return canonical


def save_key_order() -> tuple[str, ...]:
    """Key order of a save document. Tests and the spec share this tuple."""
    return _SAVE_KEYS
