"""The autouse socket block is what CI relies on."""

from __future__ import annotations

import socket

import pytest


def test_live_network_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="live network is disabled"):
        socket.create_connection(("example.com", 80), timeout=1)
    with pytest.raises(RuntimeError, match="live network is disabled"):
        socket.getaddrinfo("example.com", 443)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="live network is disabled"):
            sock.connect_ex(("1.1.1.1", 80))
    finally:
        sock.close()
