"""Own-wallet auto signer. Throwaway keys only. The mock chain does not send."""

from __future__ import annotations

import base64
import importlib.util
import logging
import traceback
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.ohlcv.registry import BONK_MINT, JTO_MINT, JUP_MINT, SOL_MINT, WIF_MINT
from app.ohlcv.store import SqliteBarStore
from app.risk.custody import RedactedSigner, SignerClosed, load_signer, sign_guarded
from app.risk.fees import reset_day_ledger, take_day
from app.risk.killfile import reset_process_halt
from app.risk.limits import (
    ALLOWLIST_MINTS,
    DAILY_LOSS_USD,
    DAILY_NOTIONAL_USD,
    MAX_OPEN_POSITIONS,
    PER_TRADE_USD,
    WALLET_CEILING_USD,
)
from app.risk.pending import pending_book
from app.risk.signer import reset_guarded
from app.venue.address import address_bytes
from app.venue.errors import VenueError
from app.venue.tx import split_transaction

_SPEC = importlib.util.spec_from_file_location(
    "tradoor_signer_helpers",
    Path(__file__).with_name("test_signer.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_helpers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_helpers)

_DAY = _helpers._DAY
_WALLET = _helpers._WALLET
_headers = _helpers._headers
_intent = _helpers._intent
_open = _helpers._open


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_day_ledger()
    reset_process_halt()
    pending_book().reset()
    reset_guarded()
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_SIGNER", raising=False)
    monkeypatch.delenv("LIVE_SIGNER_KEY_FILE", raising=False)
    monkeypatch.delenv("LIVE_KILL_FILE", raising=False)
    monkeypatch.delenv("LIVE_KILL", raising=False)
    monkeypatch.delenv("LIVE_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("LIVE_WALLET_PUBKEY", raising=False)
    monkeypatch.delenv("HOST", raising=False)


def test_limits_stay_at_the_canary_defaults() -> None:
    assert PER_TRADE_USD == Decimal("2.50")
    assert DAILY_NOTIONAL_USD == Decimal("10")
    assert DAILY_LOSS_USD == Decimal("5")
    assert MAX_OPEN_POSITIONS == 3
    assert WALLET_CEILING_USD == Decimal("25")
    assert ALLOWLIST_MINTS == frozenset({SOL_MINT, JUP_MINT, JTO_MINT, BONK_MINT, WIF_MINT})


def test_redacted_signer_hides_the_seed_in_repr_str_logs_tracebacks_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _material, seed, public = _throwaway()
    held = RedactedSigner(_material)
    caplog.set_level(logging.DEBUG)
    logging.getLogger("app.risk.custody").info("holder %s %r %s", held, held, f"{held}")
    shown = " ".join((repr(held), str(held), format(held), f"{held}", caplog.text))
    assert not _leaked(seed, shown.encode())
    path = tmp_path / "seed"
    path.write_bytes(seed + b"\xff")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_SIGNER", "local")
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(path))
    monkeypatch.setenv("LIVE_WALLET_PUBKEY", public)
    try:
        load_signer()
    except SignerClosed as exc:
        dumped = traceback.format_exc()
        assert exc.code == "signer_key_length"
        assert not _leaked(seed, str(exc).encode())
        assert not _leaked(seed, repr(exc).encode())
        assert not _leaked(seed, dumped.encode())
    else:
        raise AssertionError("a long file must disable the signer")
    assert not _leaked(seed, caplog.text.encode())
    client, rpc, store = _open(tmp_path, monkeypatch, name="redact", wallet=_WALLET)
    monkeypatch.setenv("LIVE_SIGNER", "local")
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(path))
    failed = client.post("/live/auto", json=_intent(), headers=_headers())
    assert failed.status_code == 409
    assert failed.json()["code"] == "signer_key_length"
    assert failed.json()["sent"] is False
    body = failed.content
    assert not _leaked(seed, body)
    assert not _leaked(seed, caplog.text.encode())
    assert "send" not in rpc.calls
    store.close()
    raw = (tmp_path / "redact.sqlite").read_bytes()
    assert not _leaked(seed, raw)


def test_auto_signer_is_off_unless_mode_and_trading_flag_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "absent"
    client, rpc, store = _open(tmp_path, monkeypatch, name="armed")
    monkeypatch.delenv("LIVE_SIGNER", raising=False)
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(missing))
    off = client.post("/live/auto", json=_intent(), headers=_headers())
    assert off.status_code == 409
    assert off.json()["code"] == "signer_disabled"
    assert "send" not in rpc.calls
    monkeypatch.setenv("LIVE_SIGNER", "local")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    not_exact = client.post("/live/auto", json=_intent(), headers=_headers())
    assert not_exact.json()["code"] == "trading_disabled"
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    unset = client.post("/live/auto", json=_intent(), headers=_headers())
    assert unset.json()["code"] == "trading_disabled"
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_SIGNER", "1")
    other = client.post("/live/auto", json=_intent(), headers=_headers())
    assert other.json()["code"] == "signer_disabled"
    monkeypatch.setenv("LIVE_SIGNER", "local")
    monkeypatch.setenv("HOST", "0.0.0.0")
    remote = client.post("/live/auto", json=_intent(), headers=_headers())
    assert remote.json()["code"] == "signer_disabled"
    assert "send" not in rpc.calls
    store.close()


def test_key_file_failures_disable_the_signer_without_echoing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _material, seed, public = _throwaway()
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("LIVE_SIGNER", "local")
    monkeypatch.setenv("LIVE_WALLET_PUBKEY", public)
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(tmp_path / "missing"))
    missing = _closed_code()
    assert missing == "signer_key_unreadable"
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(tmp_path))
    assert _closed_code() == "signer_key_unreadable"
    short = tmp_path / "short"
    short.write_bytes(seed[:16])
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(short))
    assert _closed_code() == "signer_key_length"
    wide = tmp_path / "wide"
    wide.write_bytes(seed + seed)
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(wide))
    assert _closed_code() == "signer_key_length"
    monkeypatch.delenv("LIVE_SIGNER_KEY_FILE", raising=False)
    assert _closed_code() == "signer_unconfigured"
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", "  ")
    assert _closed_code() == "signer_unconfigured"
    good = tmp_path / "good"
    good.write_bytes(seed)
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(good))
    monkeypatch.setenv("LIVE_WALLET_PUBKEY", _WALLET)
    assert _closed_code() == "signer_pubkey_mismatch"

    def _reject(*_args: object) -> None:
        raise ValueError("bad")

    monkeypatch.setattr(Ed25519PrivateKey, "from_private_bytes", _reject)
    monkeypatch.setenv("LIVE_WALLET_PUBKEY", public)
    assert _closed_code() == "signer_key_malformed"
    assert not _leaked(seed, caplog.text.encode())


def test_altered_or_unguarded_bytes_are_not_signed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _material, seed, public = _throwaway()
    path = tmp_path / "seed"
    path.write_bytes(seed)
    calls: list[bytes] = []
    real = RedactedSigner._sign

    def wrapped(self: RedactedSigner, message: bytes) -> bytes:
        calls.append(message)
        return real(self, message)

    monkeypatch.setattr(RedactedSigner, "_sign", wrapped)
    client, rpc, store = _armed(tmp_path, monkeypatch, path, public, name="armed")
    prepared = client.post("/live/prepare", json=_intent(), headers=_headers())
    assert prepared.status_code == 200, prepared.json()
    pending_id = prepared.json()["pending_id"]
    _sigs, message, count = split_transaction(base64.b64decode(prepared.json()["transaction_b64"]))
    assert count == 1
    held = load_signer()
    altered = bytearray(message)
    altered[-1] ^= 0x01
    with pytest.raises(SignerClosed) as refused:
        sign_guarded(held, pending_id, bytes(altered))
    assert refused.value.code == "unguarded"
    with pytest.raises(SignerClosed) as unknown:
        sign_guarded(held, 999_999, message)
    assert unknown.value.code == "unguarded"
    assert calls == []
    with pytest.raises(SignerClosed) as direct:
        held._sign(message)
    assert direct.value.code == "unguarded"
    calls.clear()
    signature = sign_guarded(held, pending_id, message)
    assert calls == [message]
    Ed25519PublicKey.from_public_bytes(address_bytes(public)).verify(signature, message)
    done = client.post("/live/auto", json=_intent(), headers=_headers())
    assert done.status_code == 409
    assert done.json()["code"] == "in_flight"
    encoded = base64.b64encode(b"\x01" + signature + message).decode("ascii")
    confirmed = client.post(
        "/live/confirm",
        json={"pending_id": pending_id, "signed_transaction_b64": encoded},
        headers=_headers(),
    )
    assert confirmed.status_code == 200, confirmed.json()
    assert confirmed.json()["sent"] is True
    with pytest.raises(SignerClosed) as again:
        sign_guarded(held, pending_id, message)
    assert again.value.code == "unguarded"
    assert "send" in rpc.calls
    assert not getattr(client.app.state.live_context.venue, "executed", False)
    store.close()
    assert not _leaked(seed, (tmp_path / "armed.sqlite").read_bytes())


def test_auto_path_books_the_day_before_send_and_keeps_it_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _material, seed, public = _throwaway()
    path = tmp_path / "seed"
    path.write_bytes(seed)
    client, rpc, store = _armed(tmp_path, monkeypatch, path, public, name="sendfail")

    def timeout(_tx: bytes) -> str:
        raise VenueError("timeout")

    rpc.send = timeout  # type: ignore[method-assign]
    failed = client.post("/live/auto", json=_intent(), headers=_headers())
    assert failed.status_code == 503
    assert failed.json()["code"] == "send_failed"
    assert failed.json()["sent"] is False
    assert not _leaked(seed, failed.content)
    store.close()
    fresh = SqliteBarStore(tmp_path / "sendfail.sqlite")
    booked = take_day(_DAY, fresh)
    assert booked.notional_usd == Decimal("2.5")
    assert booked.network_lamports == 5_000
    assert booked.priority_lamports == 1_000
    assert booked.rent_lamports == 1_000_000
    fresh.close()
    assert not _leaked(seed, (tmp_path / "sendfail.sqlite").read_bytes())
    crashed, crash_rpc, crash_store = _armed(tmp_path, monkeypatch, path, public, name="crash")

    def die(_tx: bytes) -> str:
        raise KeyboardInterrupt

    crash_rpc.send = die  # type: ignore[method-assign]
    with pytest.raises(BaseException):
        crashed.post("/live/auto", json=_intent(), headers=_headers())
    crash_store.close()
    reopened = SqliteBarStore(tmp_path / "crash.sqlite")
    kept = take_day(_DAY, reopened)
    assert kept.notional_usd == Decimal("2.5")
    assert kept.fee_total_lamports() == 1_006_000
    reopened.close()
    assert not _leaked(seed, (tmp_path / "crash.sqlite").read_bytes())


def test_auto_path_runs_the_guard_and_signs_only_a_guarded_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _material, seed, public = _throwaway()
    path = tmp_path / "seed"
    path.write_bytes(seed)
    rich, rich_rpc, rich_store = _armed(
        tmp_path,
        monkeypatch,
        path,
        public,
        name="ceiling",
        wallet_lamports=1_000_000_000_000,
    )
    blocked = rich.post("/live/auto", json=_intent(), headers=_headers())
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "wallet_ceiling"
    assert "send" not in rich_rpc.calls
    rich_store.close()
    foreign, foreign_rpc, foreign_store = _armed(
        tmp_path,
        monkeypatch,
        path,
        public,
        name="mint",
        output_mint=_WALLET,
    )
    refused = foreign.post("/live/auto", json=_intent(), headers=_headers())
    assert refused.status_code == 422
    assert refused.json()["code"] == "allowlist"
    assert "send" not in foreign_rpc.calls
    foreign_store.close()
    client, rpc, store = _armed(tmp_path, monkeypatch, path, public, name="sent")
    done = client.post("/live/auto", json=_intent(), headers=_headers())
    assert done.status_code == 200, done.json()
    assert done.json()["sent"] is True
    assert done.json()["confirmed"] is True
    assert "transaction_b64" not in done.json()
    assert not _leaked(seed, done.content)
    sigs, message, count = split_transaction(rpc.sent[0])
    assert count == 1
    Ed25519PublicKey.from_public_bytes(address_bytes(public)).verify(sigs, message)
    assert not getattr(client.app.state.live_context.venue, "executed", False)
    ledger = take_day(_DAY, store)
    assert ledger.notional_usd == Decimal("2.5")
    store.close()
    assert not _leaked(seed, (tmp_path / "sent.sqlite").read_bytes())


def _armed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    public: str,
    **kwargs: object,
) -> object:
    monkeypatch.setenv("LIVE_SIGNER", "local")
    monkeypatch.setenv("LIVE_SIGNER_KEY_FILE", str(path))
    return _open(tmp_path, monkeypatch, wallet=public, **kwargs)


def _throwaway() -> tuple[Ed25519PrivateKey, bytes, str]:
    material = Ed25519PrivateKey.generate()
    seed = material.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = material.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    from app.venue.address import encode

    return material, seed, encode(public)


def _closed_code() -> str:
    try:
        load_signer()
    except SignerClosed as exc:
        return exc.code
    raise AssertionError("signer must fail closed")


def _leaked(seed: bytes, blob: bytes) -> bool:
    return seed in blob or seed.hex().encode() in blob or base64.b64encode(seed) in blob
