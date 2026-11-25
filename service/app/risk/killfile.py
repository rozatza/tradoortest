"""Kill flag a signer can read without the API, the DB, or a token.

The only clear contents are the exact bytes ``0`` or ``0\\n``. A missing
path, a missing file, an unreadable file, or any other contents halt.
``LIVE_KILL=1`` halts as well. Nothing here opens sqlite or reads the
control token.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_CLEAR = (b"0", b"0\n")
_process_halt = False


def mark_process_halt() -> None:
    """In-process halt used when the file cannot be written."""
    global _process_halt
    _process_halt = True


def reset_process_halt() -> None:
    """Test helper. Production does not clear a halt this way."""
    global _process_halt
    _process_halt = False


def process_halted() -> bool:
    return _process_halt


def kill_file_path(environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    raw = env.get("LIVE_KILL_FILE")
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return None
    return Path(text)


def read_kill_state(path: Path) -> str:
    """``clear``, ``halt``, or ``unreadable``. Does not follow a long body."""
    try:
        with path.open("rb") as handle:
            data = handle.read(8)
            extra = handle.read(1)
    except OSError:
        return "unreadable"
    if extra or data not in _CLEAR:
        return "halt"
    return "clear"


def kill_halted(
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True unless trading is explicitly clear. Fail closed."""
    if _process_halt:
        return True
    env = os.environ if environ is None else environ
    if env.get("LIVE_KILL") == "1":
        return True
    chosen = kill_file_path(env) if path is None else path
    if chosen is None:
        return True
    return read_kill_state(chosen) != "clear"


def write_kill_halt(path: Path) -> bool:
    """Replace the file with ``1``. False when the write fails."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(b"1")
        os.replace(temporary, path)
    except OSError:
        mark_process_halt()
        return False
    return True
