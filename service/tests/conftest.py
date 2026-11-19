"""Test-wide guards.

OHLCV tests use fixtures and an in-memory HTTP transport. A live socket
from any test is a failure, including one that forgot to stub the client.
"""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def _block_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("live network is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
