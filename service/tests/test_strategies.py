"""Strategy storage, versioned saves, and the diff API. No live socket."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import LOCAL_USER_ID, CurrentUser, get_current_user
from app.dsl.format import format_strategy
from app.ir.canonical import canonical_json, ir_hash
from app.ir.validate import REPO_ROOT
from app.main import create_app
from app.strategies import routes
from app.strategies.compile import compile_body
from app.strategies.diff import dsl_unified_diff, ir_structural_diff
from app.strategies.errors import StoreUnavailable
from app.strategies.store import StrategyStore

EXAMPLES = REPO_ROOT / "schemas" / "examples"
_HEX = "ab" * 16
_SENTINEL = "917351.25"


@contextmanager
def _client(
    path: Path,
    *,
    raise_server_exceptions: bool = True,
    clock: Any = None,
    busy_timeout_ms: int = 5000,
) -> Iterator[tuple[TestClient, StrategyStore, Any]]:
    store = StrategyStore(path, busy_timeout_ms=busy_timeout_ms, clock=clock)
    application = create_app(strategies=store)
    client = TestClient(application, raise_server_exceptions=raise_server_exceptions)
    try:
        yield client, store, application
    finally:
        store.close()


def _ema() -> tuple[str, dict[str, Any]]:
    document = json.loads((EXAMPLES / "ema-cross.json").read_text(encoding="utf-8"))
    return format_strategy(document), document


def _rsi() -> str:
    document = json.loads((EXAMPLES / "rsi-cross.json").read_text(encoding="utf-8"))
    return format_strategy(document)


def _commented(dsl: str) -> str:
    return "# note\n" + dsl


def _warmup(dsl: str, bars: int) -> str:
    old = "warmup_bars: 26"
    assert old in dsl
    return dsl.replace(old, f"warmup_bars: {bars}", 1)


def _error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert error["code"] == code
    assert "message" in error and "path" in error
    assert "Traceback" not in response.text
    assert "sqlite" not in response.text.lower()
    assert "OperationalError" not in response.text
    assert 'File "' not in response.text
    return error


def _rows(path: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path) as conn:
        return list(conn.execute(sql))


def _invoke(
    app: Any,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    query: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    receive: Any = None,
) -> tuple[int, bytes]:
    header_list = list(headers or [])
    if body is not None:
        if not any(name == b"content-type" for name, _value in header_list):
            header_list.append((b"content-type", b"application/json"))
        if not any(name == b"content-length" for name, _value in header_list):
            header_list.append((b"content-length", str(len(body)).encode("ascii")))
    sent: list[dict[str, Any]] = []

    async def default_receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body or b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "headers": header_list,
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive or default_receive, send))
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, raw


def test_pragmas_lock_and_foreign_keys(tmp_path: Path) -> None:
    store = StrategyStore(tmp_path / "strategies.sqlite", busy_timeout_ms=2500)
    try:
        assert isinstance(store._lock, type(threading.RLock()))
        assert isinstance(routes._init_lock, type(threading.Lock()))
        assert store._conn is not None
        assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2500
        tables = {
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert tables == {"strategies", "strategy_versions"}
        forbidden = {"open", "high", "low", "close", "volume", "bars", "ohlcv", "mint"}
        for table in tables:
            columns = {row[1] for row in store._conn.execute(f"PRAGMA table_info({table})")}
            assert columns.isdisjoint(forbidden)
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                """
                INSERT INTO strategy_versions (
                    strategy_id, version, user_id, dsl, ir_json, ir_hash, created_at
                ) VALUES (?, 1, 'local-user', 'x', '{}', ?, '2026-01-01T00:00:00Z')
                """,
                (_HEX, "ab" * 32),
            )
    finally:
        store.close()


def test_structural_diff_is_independent_of_insertion_order() -> None:
    left_first: dict[str, Any] = {}
    left_first["b"] = 1
    left_first["a"] = [0, {"z": 1, "y": 2}]
    right_first: dict[str, Any] = {}
    right_first["a"] = [0, {"y": 3, "z": 1}, 4]
    right_first["b"] = 1
    left_second: dict[str, Any] = {"a": [0, {"y": 2, "z": 1}], "b": 1}
    right_second: dict[str, Any] = {"b": 1, "a": [0, {"z": 1, "y": 3}, 4]}
    expected = [
        {"op": "replace", "path": "$.a[1].y", "from": 2, "to": 3},
        {"op": "add", "path": "$.a[2]", "value": 4},
    ]
    assert ir_structural_diff(left_first, right_first) == expected
    assert ir_structural_diff(left_second, right_second) == expected
    before = list(range(12))
    after = list(range(12))
    after[10] = 99
    after[2] = 7
    paths = [item["path"] for item in ir_structural_diff(before, after)]
    assert paths == ["$[2]", "$[10]"]
    assert dsl_unified_diff("same\n", "same\n", 1, 1) == ""


def test_health_does_not_open_the_strategy_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    application = create_app()
    assert application.state.strategies is None
    response = TestClient(application).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert application.state.strategies is None
    assert not (tmp_path / "data" / "strategies.sqlite").exists()


def test_gitignore_covers_the_strategy_file() -> None:
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in text.splitlines()
    assert "*.sqlite" in text.splitlines()


def test_create_list_get_and_owner_are_the_local_user(tmp_path: Path) -> None:
    dsl, document = _ema()
    ticks = iter(range(1, 10))

    def clock() -> str:
        return f"2026-01-01T00:00:{next(ticks):02d}Z"

    with _client(tmp_path / "strategies.sqlite", clock=clock) as (client, store, _app):
        created = client.post(
            "/strategies",
            json={"user_id": "someone-else", "bars": {"sol": [{"close": _SENTINEL}]}, "dsl": dsl},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert created.headers["location"] == f"/strategies/{body['strategy_id']}"
        assert body["user_id"] == LOCAL_USER_ID
        assert body["ir_strategy_id"] == "ema_cross_sol"
        assert body["latest_version"] == 1
        assert body["archived"] is False
        assert body["version"]["version"] == 1
        assert body["version"]["dsl"] == dsl
        assert body["version"]["ir_hash"] == ir_hash(document)
        assert body["ir_hash"] == body["version"]["ir_hash"]
        assert body["version"]["ir"] == json.loads(canonical_json(document))
        stored = _rows(
            store.path,
            "SELECT user_id, ir_json, ir_hash, dsl FROM strategy_versions",
        )
        assert stored == [(LOCAL_USER_ID, canonical_json(document), ir_hash(document), dsl)]
        blob = store.path.read_bytes()
        assert _SENTINEL.encode() not in blob
        assert b"someone-else" not in blob

        other = compile_body(json.dumps({"dsl": _rsi()}).encode("utf-8"))
        foreign, _version = store.create("someone-else", other)
        listed = client.get("/strategies")
        assert listed.status_code == 200
        page = listed.json()
        assert page["total"] == 1
        assert page["limit"] == 50
        assert page["offset"] == 0
        assert [item["strategy_id"] for item in page["strategies"]] == [body["strategy_id"]]
        missing = client.get(f"/strategies/{foreign.strategy_id}")
        _error(missing, 404, "not_found")


def test_pagination_order_and_bounds(tmp_path: Path) -> None:
    ema, _document = _ema()
    rsi = _rsi()
    ticks = iter(range(1, 20))

    def clock() -> str:
        return f"2026-01-01T00:00:{next(ticks):02d}Z"

    with _client(tmp_path / "strategies.sqlite", clock=clock) as (client, _store, _app):
        first = client.post("/strategies", json={"dsl": ema}).json()
        second = client.post("/strategies", json={"dsl": rsi}).json()
        third = client.post("/strategies", json={"dsl": ema}).json()
        page = client.get("/strategies?limit=2&offset=0")
        assert page.status_code == 200
        body = page.json()
        assert body["total"] == 3
        assert [item["strategy_id"] for item in body["strategies"]] == [
            first["strategy_id"],
            second["strategy_id"],
        ]
        rest = client.get("/strategies?limit=2&offset=2").json()
        assert [item["strategy_id"] for item in rest["strategies"]] == [third["strategy_id"]]
        empty = client.get("/strategies?limit=100&offset=10000")
        assert empty.status_code == 200
        assert empty.json()["strategies"] == []
        assert empty.json()["total"] == 3

        rejected = [
            "limit=0",
            "limit=101",
            "limit=-1",
            "limit=true",
            "limit=1.5",
            "limit=01",
            "offset=-1",
            "offset=10001",
            "offset=false",
            "include_archived=yes",
            "include_archived=1",
            "limit=1&limit=2",
        ]
        for query in rejected:
            response = client.get(f"/strategies?{query}")
            error = _error(response, 422, "invalid_query")
            assert error["message"]
        huge = "9" * 5000
        reflected = client.get(f"/strategies?limit={huge}")
        _error(reflected, 422, "invalid_query")
        assert huge not in reflected.text
        assert len(reflected.text) < 400


def test_versions_are_immutable_and_hashes_follow_the_ir(tmp_path: Path) -> None:
    dsl, document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        assert created.status_code == 201, created.text
        strategy_id = created.json()["strategy_id"]
        original_hash = created.json()["version"]["ir_hash"]
        assert original_hash == ir_hash(document)

        commented = client.put(f"/strategies/{strategy_id}", json={"dsl": _commented(dsl)})
        assert commented.status_code == 200, commented.text
        assert commented.json()["latest_version"] == 2
        assert commented.json()["version"]["ir_hash"] == original_hash
        assert commented.json()["version"]["dsl"] == _commented(dsl)
        assert commented.json()["name"] == "EMA cross"

        changed = client.put(f"/strategies/{strategy_id}", json={"dsl": _warmup(dsl, 30)})
        assert changed.status_code == 200, changed.text
        assert changed.json()["latest_version"] == 3
        assert changed.json()["version"]["ir_hash"] != original_hash
        renamed = dsl.replace('"EMA cross"', '"EMA renamed"', 1)
        renamed_response = client.put(f"/strategies/{strategy_id}", json={"dsl": renamed})
        assert renamed_response.status_code == 200
        assert renamed_response.json()["name"] == "EMA renamed"

        first = client.get(f"/strategies/{strategy_id}/versions/1")
        assert first.status_code == 200
        assert first.json()["dsl"] == dsl
        assert first.json()["ir_hash"] == original_hash
        assert first.json()["ir"]["strategy"]["name"] == "EMA cross"
        latest = client.get(f"/strategies/{strategy_id}")
        assert latest.json()["version"]["version"] == 4
        assert latest.json()["version"]["ir"]["strategy"]["name"] == "EMA renamed"
        listed = client.get(f"/strategies/{strategy_id}/versions?limit=2&offset=1")
        assert listed.status_code == 200
        summary = listed.json()
        assert summary["total"] == 4
        assert [item["version"] for item in summary["versions"]] == [2, 3]
        hashes = {item["version"]: item["ir_hash"] for item in summary["versions"]}
        assert hashes[2] == original_hash
        assert hashes[3] != original_hash

        rows = _rows(
            store.path,
            "SELECT version, ir_hash, dsl FROM strategy_versions ORDER BY version",
        )
        assert [row[0] for row in rows] == [1, 2, 3, 4]
        assert rows[0][1] == rows[1][1]
        assert rows[0][2] == dsl
        assert "warmup_bars: 30" in rows[2][2]


def test_diff_forward_reverse_and_identical(tmp_path: Path) -> None:
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, _store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        first_hash = created.json()["version"]["ir_hash"]
        client.put(f"/strategies/{strategy_id}", json={"dsl": _commented(dsl)})
        changed = client.put(f"/strategies/{strategy_id}", json={"dsl": _warmup(dsl, 30)})
        second_hash = changed.json()["version"]["ir_hash"]

        forward = client.get(f"/strategies/{strategy_id}/diff?from=1&to=3")
        assert forward.status_code == 200, forward.text
        body = forward.json()
        assert body["from_version"] == 1
        assert body["to_version"] == 3
        assert body["from_ir_hash"] == first_hash
        assert body["to_ir_hash"] == second_hash
        assert body["dsl_diff"].startswith("--- v1\n+++ v3\n")
        assert "-  warmup_bars: 26\n" in body["dsl_diff"] or "-warmup_bars: 26" in body["dsl_diff"]
        assert "warmup_bars: 30" in body["dsl_diff"]
        assert body["ir_diff"] == [
            {"op": "replace", "path": "$.engine.warmup_bars", "from": 26, "to": 30}
        ]

        backward = client.get(f"/strategies/{strategy_id}/diff?from=3&to=1")
        assert backward.status_code == 200
        flipped = backward.json()
        assert flipped["dsl_diff"].startswith("--- v3\n+++ v1\n")
        assert flipped["from_ir_hash"] == second_hash
        assert flipped["to_ir_hash"] == first_hash
        assert flipped["ir_diff"] == [
            {"op": "replace", "path": "$.engine.warmup_bars", "from": 30, "to": 26}
        ]

        same = client.get(f"/strategies/{strategy_id}/diff?from=1&to=1")
        assert same.status_code == 200
        assert same.json()["from_ir_hash"] == same.json()["to_ir_hash"] == first_hash
        assert same.json()["dsl_diff"] == ""
        assert same.json()["ir_diff"] == []

        comment = client.get(f"/strategies/{strategy_id}/diff?from=1&to=2")
        assert comment.json()["from_ir_hash"] == comment.json()["to_ir_hash"] == first_hash
        assert comment.json()["ir_diff"] == []
        assert "# note" in comment.json()["dsl_diff"]

        missing = client.get(f"/strategies/{strategy_id}/diff?from=1&to=9")
        _error(missing, 404, "not_found")
        for query in ("", "from=1", "to=2", "from=0&to=1", "from=1&to=foo", "from=1.5&to=2"):
            bad = client.get(f"/strategies/{strategy_id}/diff?{query}")
            _error(bad, 422, "invalid_query")
        too_far = client.get(f"/strategies/{strategy_id}/versions/1000001")
        _error(too_far, 422, "invalid_query")
        absent = client.get(f"/strategies/{strategy_id}/versions/1000000")
        _error(absent, 404, "not_found")


def test_ir_input_formats_dsl_and_matches_the_hash(tmp_path: Path) -> None:
    dsl, document = _ema()
    shuffled = json.loads(json.dumps(document))
    shuffled["costs"] = {"slippage_bps": 5, "commission_bps": 10.0}
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"ir": shuffled, "user_id": {"ignored": True}})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["version"]["ir_hash"] == ir_hash(document)
        assert body["version"]["dsl"] == dsl
        assert body["version"]["ir"]["costs"]["commission_bps"] == 10
        stored = _rows(store.path, "SELECT ir_json FROM strategy_versions")
        assert stored[0][0] == canonical_json(document)


def test_invalid_dsl_and_ir_write_nothing(tmp_path: Path) -> None:
    dsl, _document = _ema()
    missing = json.loads(
        (Path(__file__).parent / "fixtures" / "invalid" / "missing-entry.json").read_text(
            encoding="utf-8"
        )
    )
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        before = _rows(store.path, "SELECT version, dsl FROM strategy_versions")

        syntax = client.post("/strategies", json={"dsl": "not a strategy\n"})
        error = _error(syntax, 422, "syntax")
        assert error["diagnostics"][0]["line"] == 1
        assert "diagnostics" in syntax.json()["error"]

        both = client.post("/strategies", json={"dsl": dsl, "ir": missing})
        _error(both, 422, "invalid_request")
        neither = client.post("/strategies", json={"user_id": "local-user"})
        _error(neither, 422, "invalid_request")
        for payload in ({"dsl": 1}, {"dsl": True}, {"ir": []}, {"ir": "text"}):
            rejected = client.post("/strategies", json=payload)
            _error(rejected, 422, "invalid_request")

        invalid = client.put(f"/strategies/{strategy_id}", json={"ir": missing})
        assert invalid.status_code == 422, invalid.text
        error = _error(invalid, 422, invalid.json()["error"]["code"])
        assert error["diagnostics"]
        assert error["code"] == error["diagnostics"][0]["code"]
        assert _rows(store.path, "SELECT version, dsl FROM strategy_versions") == before
        assert client.get(f"/strategies/{strategy_id}").json()["latest_version"] == 1

        lone = client.post(
            "/strategies",
            content=b'{"dsl": "\\ud800"}',
            headers={"content-type": "application/json"},
        )
        _error(lone, 422, "invalid_string")
        assert _rows(store.path, "SELECT COUNT(*) FROM strategies") == [(1,)]


def test_malformed_bodies_are_422(tmp_path: Path) -> None:
    cases: list[tuple[bytes, str]] = [
        (b"{", "invalid_json"),
        (b"[]", "invalid_json"),
        (b"null", "invalid_json"),
        (b"\xff", "invalid_json"),
        ("{}".encode("utf-16"), "invalid_json"),
        (b'{"dsl": NaN}', "invalid_json"),
        (b'{"dsl": Infinity}', "invalid_json"),
        (b'{"dsl": -Infinity}', "invalid_json"),
        (b'{"ir": 1e400}', "invalid_json"),
        (b'{"dsl": "ok\x00"}', "invalid_json"),
        (b'{"dsl": "ok\\u0000"}', "invalid_string"),
        (b'{"\\ud800": "x"}', "invalid_string"),
    ]
    with _client(tmp_path / "strategies.sqlite", raise_server_exceptions=False) as (
        client,
        store,
        _app,
    ):
        for raw, code in cases:
            response = client.post(
                "/strategies",
                content=raw,
                headers={"content-type": "application/json"},
            )
            _error(response, 422, code)
        nested: dict[str, Any] = {}
        cursor = nested
        for _ in range(100):
            cursor["child"] = {}
            cursor = cursor["child"]
        deep = client.post("/strategies", json={"dsl": "x", "nested": nested})
        _error(deep, 422, "expr_too_deep")
        assert _rows(store.path, "SELECT COUNT(*) FROM strategies") == [(0,)]


def test_archive_is_hidden_on_every_addressed_route(tmp_path: Path) -> None:
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        client.put(f"/strategies/{strategy_id}", json={"dsl": _warmup(dsl, 30)})
        archived = client.delete(f"/strategies/{strategy_id}")
        assert archived.status_code == 200, archived.text
        body = archived.json()
        assert body["archived"] is True
        assert body["archived_at"]
        assert "dsl" not in body
        assert body["latest_version"] == 2

        addressed = [
            ("GET", f"/strategies/{strategy_id}", None),
            ("PUT", f"/strategies/{strategy_id}", {"dsl": dsl}),
            ("DELETE", f"/strategies/{strategy_id}", None),
            ("GET", f"/strategies/{strategy_id}/versions", None),
            ("GET", f"/strategies/{strategy_id}/versions/1", None),
            ("GET", f"/strategies/{strategy_id}/diff?from=1&to=2", None),
        ]
        for method, path, payload in addressed:
            response = client.request(method, path, json=payload)
            _error(response, 404, "not_found")
        listed = client.get("/strategies")
        assert listed.json()["strategies"] == []
        included = client.get("/strategies?include_archived=true")
        assert included.status_code == 200
        assert included.json()["total"] == 1
        assert included.json()["strategies"][0]["archived"] is True
        assert "dsl" not in included.json()["strategies"][0]
        assert _rows(store.path, "SELECT COUNT(*) FROM strategy_versions") == [(2,)]
        fresh = client.post("/strategies", json={"dsl": dsl})
        assert fresh.status_code == 201


def test_bad_ids_and_versions_do_not_echo_the_input(tmp_path: Path) -> None:
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, _store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        long_id = "a" * 5000
        response = client.get(f"/strategies/{long_id}")
        _error(response, 422, "invalid_id")
        assert long_id not in response.text
        assert len(response.text) < 400
        upper = client.get(f"/strategies/{strategy_id.upper()}")
        _error(upper, 422, "invalid_id")
        short = client.get("/strategies/" + "ab" * 15)
        _error(short, 422, "invalid_id")
        for version in ("0", "01", "-1", "1.2", "true", "x"):
            bad = client.get(f"/strategies/{strategy_id}/versions/{version}")
            _error(bad, 422, "invalid_query")


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/strategies", None),
        ("GET", "/strategies?limit=-1", None),
        ("POST", "/strategies", {}),
        ("GET", f"/strategies/{_HEX}", None),
        ("PUT", f"/strategies/{_HEX}", {}),
        ("DELETE", f"/strategies/{_HEX}", None),
        ("GET", f"/strategies/{_HEX}/versions", None),
        ("GET", f"/strategies/{_HEX}/versions/nope", None),
        ("GET", f"/strategies/{_HEX}/diff?from=1&to=1", None),
        ("GET", "/strategies/not-an-id", None),
    ],
)
def test_corrupt_database_is_503_on_every_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a database")
    monkeypatch.setenv("STRATEGIES_DB_PATH", str(corrupt))
    application = create_app()
    client = TestClient(application, raise_server_exceptions=False)
    response = client.request(method, path, json=payload)
    _error(response, 503, "store_unavailable")
    assert str(corrupt) not in response.text
    assert "not a database" not in response.text
    assert application.state.strategies is None


def test_failed_open_does_not_poison_app_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a database")
    monkeypatch.setenv("STRATEGIES_DB_PATH", str(corrupt))
    application = create_app()
    client = TestClient(application, raise_server_exceptions=False)
    failed = client.get("/strategies")
    _error(failed, 503, "store_unavailable")
    assert application.state.strategies is None

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("STRATEGIES_DB_PATH", str(blocker / "strategies.sqlite"))
    blocked = client.get("/strategies")
    _error(blocked, 503, "store_unavailable")
    assert str(blocker) not in blocked.text
    assert application.state.strategies is None

    monkeypatch.setenv("STRATEGIES_DB_PATH", str(tmp_path / "ok" / "strategies.sqlite"))
    monkeypatch.setenv("STRATEGIES_BUSY_TIMEOUT_MS", "nope")
    bad_env = client.get("/strategies")
    _error(bad_env, 503, "store_unavailable")
    assert "nope" not in bad_env.text
    assert application.state.strategies is None

    monkeypatch.delenv("STRATEGIES_BUSY_TIMEOUT_MS")
    recovered = client.get("/strategies")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["strategies"] == []
    assert isinstance(application.state.strategies, StrategyStore)
    application.state.strategies.close()


def test_bad_body_cap_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATEGIES_DB_PATH", str(tmp_path / "strategies.sqlite"))
    monkeypatch.setenv("STRATEGIES_MAX_BODY_BYTES", "nope")
    application = create_app()
    client = TestClient(application, raise_server_exceptions=False)
    failed = client.post("/strategies", content=b"{}")
    _error(failed, 503, "store_unavailable")
    assert "nope" not in failed.text
    monkeypatch.setenv("STRATEGIES_MAX_BODY_BYTES", "128")
    rejected = client.post("/strategies", content=b"{}")
    _error(rejected, 422, "invalid_request")
    assert isinstance(application.state.strategies, StrategyStore)
    application.state.strategies.close()


def test_locked_and_closed_store_are_503(tmp_path: Path) -> None:
    path = tmp_path / "strategies.sqlite"
    store = StrategyStore(path, busy_timeout_ms=0)
    application = create_app(strategies=store)
    client = TestClient(application, raise_server_exceptions=False)
    other = sqlite3.connect(path, isolation_level=None, timeout=0)
    other.execute("BEGIN EXCLUSIVE")
    try:
        locked = client.get("/strategies")
        _error(locked, 503, "store_unavailable")
        assert "locked" not in locked.text.lower()
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert client.get("/strategies").status_code == 200
    store.close()
    closed = client.get("/strategies")
    _error(closed, 503, "store_unavailable")
    assert "closed" not in closed.text.lower()
    assert client.get("/health").status_code == 200


def test_unexpected_errors_use_the_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path / "strategies.sqlite", raise_server_exceptions=False) as (
        client,
        _store,
        _app,
    ):

        def explode(self: StrategyStore, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("secret /tmp/strategies.sqlite boom")

        monkeypatch.setattr(StrategyStore, "list_for", explode)
        failed = client.get("/strategies")
        _error(failed, 500, "internal")
        assert "secret" not in failed.text
        assert ".sqlite" not in failed.text

        def locked(self: StrategyStore, *_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked: /tmp/secret.sqlite")

        monkeypatch.setattr(StrategyStore, "list_for", locked)
        unavailable = client.get("/strategies")
        _error(unavailable, 503, "store_unavailable")
        assert "secret" not in unavailable.text
        assert "locked" not in unavailable.text.lower()


def test_declared_and_chunked_bodies_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRATEGIES_MAX_BODY_BYTES", "32")
    with _client(tmp_path / "strategies.sqlite", raise_server_exceptions=False) as (
        client,
        _store,
        application,
    ):
        oversized = client.post(
            "/strategies",
            content=b'{"dsl":"' + b"a" * 40 + b'"}',
            headers={"content-type": "application/json"},
        )
        error = _error(oversized, 413, "payload_too_large")
        assert error["path"] is None
        assert b"aaaa" not in oversized.content

        state = {"calls": 0}

        async def receive() -> dict[str, Any]:
            state["calls"] += 1
            if state["calls"] == 1:
                return {"type": "http.request", "body": b"x" * 20, "more_body": True}
            if state["calls"] == 2:
                return {"type": "http.request", "body": b"y" * 20, "more_body": True}
            raise AssertionError("the tail of a capped body was read")

        status, raw = _invoke(
            application,
            "POST",
            "/strategies",
            headers=[(b"content-type", b"application/json")],
            receive=receive,
        )
        assert status == 413
        assert json.loads(raw)["error"]["code"] == "payload_too_large"
        assert state["calls"] == 2
        assert b"yyyy" not in raw

        declared = {"called": False}

        async def unread() -> dict[str, Any]:
            declared["called"] = True
            return {"type": "http.request", "body": b"{}", "more_body": False}

        status, raw = _invoke(
            application,
            "POST",
            "/strategies",
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", b"999999"),
            ],
            receive=unread,
        )
        assert status == 413
        assert declared["called"] is False
        assert json.loads(raw)["error"]["code"] == "payload_too_large"

        status, raw = _invoke(
            application,
            "POST",
            "/strategies",
            headers=[(b"content-length", b"abc")],
            body=b"{}",
        )
        assert status == 400
        assert json.loads(raw)["error"]["code"] == "invalid_request"


def test_failed_insert_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsl, _document = _ema()
    store = StrategyStore(tmp_path / "strategies.sqlite")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_insert_version", boom)
    try:
        with pytest.raises(StoreUnavailable):
            store.create(LOCAL_USER_ID, compile_body(json.dumps({"dsl": dsl}).encode("utf-8")))
        assert store._conn is not None
        assert store._conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
        assert store._conn.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()[0] == 0
    finally:
        store.close()


def test_a_saved_strategy_survives_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / "data" / "strategies.sqlite"
    monkeypatch.setenv("STRATEGIES_DB_PATH", str(destination))
    dsl, document = _ema()
    first = create_app()
    created = TestClient(first).post("/strategies", json={"dsl": dsl})
    assert created.status_code == 201
    strategy_id = created.json()["strategy_id"]
    digest = created.json()["version"]["ir_hash"]
    assert digest == ir_hash(document)
    assert isinstance(first.state.strategies, StrategyStore)
    first.state.strategies.close()

    second = create_app()
    again = TestClient(second).get(f"/strategies/{strategy_id}")
    assert again.status_code == 200
    assert again.json()["version"]["ir_hash"] == digest
    assert again.json()["version"]["dsl"] == dsl
    assert isinstance(second.state.strategies, StrategyStore)
    second.state.strategies.close()


def test_other_current_user_is_forbidden(tmp_path: Path) -> None:
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, _store, application):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        name = created.json()["name"]
        application.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id="other")
        paths = [
            ("GET", "/strategies"),
            ("POST", "/strategies"),
            ("GET", f"/strategies/{strategy_id}"),
            ("PUT", f"/strategies/{strategy_id}"),
            ("DELETE", f"/strategies/{strategy_id}"),
            ("GET", f"/strategies/{strategy_id}/versions"),
            ("GET", f"/strategies/{strategy_id}/versions/1"),
            ("GET", f"/strategies/{strategy_id}/diff?from=1&to=1"),
            ("POST", f"/strategies/{strategy_id}/validate"),
        ]
        for method, path in paths:
            response = client.request(method, path, json={"dsl": dsl})
            _error(response, 403, "forbidden")
            assert name not in response.text
            assert strategy_id not in response.text


def test_save_does_not_block_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsl, _document = _ema()
    entered = threading.Event()
    release = threading.Event()
    original = StrategyStore.create

    def blocking(self: StrategyStore, user_id: str, compiled: Any) -> Any:
        entered.set()
        assert release.wait(3)
        return original(self, user_id, compiled)

    monkeypatch.setattr(StrategyStore, "create", blocking)
    with _client(tmp_path / "strategies.sqlite") as (client, _store, _app):
        holder: dict[str, Any] = {}

        def post() -> None:
            holder["response"] = client.post("/strategies", json={"dsl": dsl})

        worker = threading.Thread(target=post)
        worker.start()
        assert entered.wait(2)
        health: dict[str, Any] = {}

        def get_health() -> None:
            started = time.perf_counter()
            health["response"] = client.get("/health")
            health["elapsed"] = time.perf_counter() - started

        try:
            checker = threading.Thread(target=get_health)
            checker.start()
            checker.join(0.3)
            blocked = checker.is_alive()
            release.set()
            checker.join(2)
            worker.join(2)
        finally:
            release.set()
        assert not blocked
        assert not checker.is_alive()
        assert not worker.is_alive()
        response = health["response"]
        assert isinstance(response, httpx.Response)
        assert response.status_code == 200
        assert float(health["elapsed"]) < 0.3
        assert holder["response"].status_code == 201


def test_concurrent_post_put_and_get_keep_monotonic_versions(tmp_path: Path) -> None:
    dsl, _document = _ema()
    path = tmp_path / "strategies.sqlite"
    store = StrategyStore(path, busy_timeout_ms=2000)
    application = create_app(strategies=store)
    status, raw = _invoke(
        application,
        "POST",
        "/strategies",
        body=json.dumps({"dsl": dsl}).encode("utf-8"),
    )
    assert status == 201, raw
    created = json.loads(raw)
    strategy_id = created["strategy_id"]
    first_hash = created["version"]["ir_hash"]
    results: list[tuple[str, int, bytes]] = []
    lock = threading.Lock()

    def worker() -> None:
        local: list[tuple[str, int, bytes]] = []
        try:
            for _ in range(4):
                posted_status, posted = _invoke(
                    application,
                    "POST",
                    "/strategies",
                    body=json.dumps({"dsl": dsl}).encode("utf-8"),
                )
                local.append(("POST", posted_status, posted))
                if posted_status == 201:
                    new_id = json.loads(posted)["strategy_id"]
                    got_status, got = _invoke(application, "GET", f"/strategies/{new_id}")
                    local.append(("GET", got_status, got))
                put_status, put = _invoke(
                    application,
                    "PUT",
                    f"/strategies/{strategy_id}",
                    body=json.dumps({"dsl": dsl}).encode("utf-8"),
                )
                local.append(("PUT", put_status, put))
                seed_status, seed = _invoke(application, "GET", f"/strategies/{strategy_id}")
                local.append(("GET", seed_status, seed))
                started = time.perf_counter()
                health_status, health = _invoke(application, "GET", "/health")
                elapsed = time.perf_counter() - started
                local.append(("HEALTH", health_status, health))
                if elapsed >= 1.5:
                    local.append(("SLOW", 500, str(elapsed).encode("ascii")))
        except Exception as exc:  # recorded so the join still finishes
            local.append(("EXC", 500, str(exc).encode("utf-8", errors="replace")))
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert results
        put_versions: list[int] = []
        for kind, status, payload in results:
            text = payload.decode("utf-8", errors="replace")
            assert status < 500, (kind, status, text)
            assert "Traceback" not in text
            assert "sqlite" not in text.lower()
            assert "Internal Server Error" not in text
            if kind == "POST":
                assert status == 201, text
            elif kind == "PUT":
                assert status == 200, text
                put_versions.append(json.loads(payload)["latest_version"])
            elif kind == "GET":
                assert status == 200, text
            elif kind == "HEALTH":
                assert status == 200, text
                assert json.loads(payload) == {"status": "ok"}
            else:
                raise AssertionError((kind, status, text))
        assert sorted(put_versions) == list(range(2, 2 + len(put_versions)))
        assert len(put_versions) == 24
        with sqlite3.connect(path) as conn:
            numbers = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT version FROM strategy_versions
                    WHERE strategy_id = ?
                    ORDER BY version
                    """,
                    (strategy_id,),
                )
            ]
            duplicates = conn.execute(
                """
                SELECT strategy_id, version, COUNT(*)
                FROM strategy_versions
                GROUP BY strategy_id, version
                HAVING COUNT(*) > 1
                """
            ).fetchall()
        assert numbers == list(range(1, 26))
        assert duplicates == []
        status, raw = _invoke(application, "GET", f"/strategies/{strategy_id}/versions/1")
        assert status == 200
        assert json.loads(raw)["ir_hash"] == first_hash
    finally:
        store.close()


def test_parent_delete_is_rejected_by_the_foreign_key(tmp_path: Path) -> None:
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        client.post("/strategies", json={"dsl": dsl})
        with pytest.raises(sqlite3.IntegrityError):
            assert store._conn is not None
            store._conn.execute("DELETE FROM strategies")
        assert store._conn.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()[0] == 1


def test_unified_diff_marks_a_missing_trailing_newline() -> None:
    """A body line without ``\\n`` stays its own line, with the diff -u marker."""
    prefix = "engine:\n  bar_size: 1h\n"
    marker = "\\ No newline at end of file\n"

    both = dsl_unified_diff(prefix + "# end A", prefix + "# end B", 1, 2)
    assert both.startswith("--- v1\n+++ v2\n")
    assert "@@ " in both
    assert f"-# end A\n{marker}+# end B\n{marker}" in both
    assert "-# end A+" not in both

    old_missing = dsl_unified_diff(prefix + "# end A", prefix + "# end B\n", 1, 2)
    assert f"-# end A\n{marker}+# end B\n" in old_missing
    assert "+# end B\n\\ No newline" not in old_missing
    assert "-# end A+" not in old_missing

    new_missing = dsl_unified_diff(prefix + "# end A\n", prefix + "# end B", 3, 4)
    assert new_missing.startswith("--- v3\n+++ v4\n")
    assert f"-# end A\n+# end B\n{marker}" in new_missing
    assert "-# end A\n\\ No newline" not in new_missing
    assert "-# end A+" not in new_missing

    bare = prefix + "  quote_currency: USDC"
    newline_only = dsl_unified_diff(bare, bare + "\n", 1, 2)
    assert f"-  quote_currency: USDC\n{marker}+  quote_currency: USDC\n" in newline_only
    assert "-  quote_currency: USDC+" not in newline_only

    assert dsl_unified_diff(bare, bare, 1, 1) == ""
    assert dsl_unified_diff(bare + "\n", bare + "\n", 2, 2) == ""


def test_structural_diff_distinguishes_types_and_shape() -> None:
    """Kills D1, D5, D6, and D12."""
    assert ir_structural_diff(1, True) == [{"op": "replace", "path": "$", "from": 1, "to": True}]
    assert ir_structural_diff(0, False) == [{"op": "replace", "path": "$", "from": 0, "to": False}]
    assert ir_structural_diff(1, 1.0) == [{"op": "replace", "path": "$", "from": 1, "to": 1.0}]
    assert ir_structural_diff({"a": 1}, {"a": 1, "b": 2}) == [
        {"op": "add", "path": "$.b", "value": 2}
    ]
    assert ir_structural_diff({"a": 1, "b": 2}, {"a": 1}) == [
        {"op": "remove", "path": "$.b", "value": 2}
    ]
    assert ir_structural_diff([1], [1, 2]) == [{"op": "add", "path": "$[1]", "value": 2}]
    assert ir_structural_diff([1, 2], [1]) == [{"op": "remove", "path": "$[1]", "value": 2}]


def test_second_user_is_hidden_on_every_route(tmp_path: Path) -> None:
    """Kills O1. A row seeded for another owner stays hidden and unchanged."""
    dsl, _document = _ema()
    bob = "cd" * 16
    digest = "ab" * 32
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        assert created.status_code == 201, created.text
        local_id = created.json()["strategy_id"]
        assert store._conn is not None
        store._conn.execute(
            """
            INSERT INTO strategies (
                strategy_id, user_id, name, ir_strategy_id,
                created_at, updated_at, archived_at
            ) VALUES (?, 'other-user', 'other-owner-name', 'bob_sol', ?, ?, NULL)
            """,
            (bob, "2026-01-01T00:00:01Z", "2026-01-01T00:00:01Z"),
        )
        store._conn.execute(
            """
            INSERT INTO strategy_versions (
                strategy_id, version, user_id, dsl, ir_json, ir_hash, created_at
            ) VALUES (?, 1, 'other-user', 'bob-dsl', '{}', ?, ?)
            """,
            (bob, digest, "2026-01-01T00:00:01Z"),
        )

        def bob_rows() -> list[tuple[Any, ...]]:
            assert store._conn is not None
            return list(
                store._conn.execute(
                    """
                    SELECT s.user_id, s.archived_at, v.version, v.user_id, v.dsl
                    FROM strategies AS s
                    JOIN strategy_versions AS v ON v.strategy_id = s.strategy_id
                    WHERE s.strategy_id = ?
                    ORDER BY v.version
                    """,
                    (bob,),
                )
            )

        before = bob_rows()
        listed = client.get("/strategies")
        assert listed.status_code == 200
        page = listed.json()
        assert page["total"] == 1
        assert [item["strategy_id"] for item in page["strategies"]] == [local_id]
        included = client.get("/strategies?include_archived=true")
        assert included.status_code == 200
        assert included.json()["total"] == 1
        assert [item["strategy_id"] for item in included.json()["strategies"]] == [local_id]
        calls = [
            ("GET", f"/strategies/{bob}", None),
            ("PUT", f"/strategies/{bob}", {"dsl": dsl}),
            ("DELETE", f"/strategies/{bob}", None),
            ("GET", f"/strategies/{bob}/versions", None),
            ("GET", f"/strategies/{bob}/versions/1", None),
            ("GET", f"/strategies/{bob}/diff?from=1&to=1", None),
            ("POST", f"/strategies/{bob}/validate", {"dsl": dsl}),
        ]
        for method, path, payload in calls:
            response = client.request(method, path, json=payload)
            error = _error(response, 404, "not_found")
            assert error["message"] == "strategy was not found"
        assert bob_rows() == before


def test_mid_write_failure_rolls_back_and_keeps_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills M8 and M9. A failed write rolls back, then another write works."""
    dsl, _document = _ema()
    compiled = compile_body(json.dumps({"dsl": dsl}).encode("utf-8"))
    store = StrategyStore(tmp_path / "strategies.sqlite")
    try:
        store.create(LOCAL_USER_ID, compiled)
        assert _strategy_count(store) == 1

        def unavailable(*_args: object, **_kwargs: object) -> None:
            raise StoreUnavailable()

        monkeypatch.setattr(store, "_insert_version", unavailable)
        with pytest.raises(StoreUnavailable):
            store.create(LOCAL_USER_ID, compiled)
        assert _strategy_count(store) == 1
        monkeypatch.undo()

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("mid-write")

        monkeypatch.setattr(store, "_insert_version", boom)
        with pytest.raises(RuntimeError, match="mid-write"):
            store.create(LOCAL_USER_ID, compiled)
        assert _strategy_count(store) == 1
        monkeypatch.undo()

        store.create(LOCAL_USER_ID, compiled)
        assert _strategy_count(store) == 2
    finally:
        store.close()


def test_body_cap_exact_boundary_and_duplicate_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills B3, B4, B5, B7, and B9."""
    monkeypatch.setenv("STRATEGIES_MAX_BODY_BYTES", "32")
    with _client(tmp_path / "strategies.sqlite", raise_server_exceptions=False) as (
        _client_http,
        _store,
        application,
    ):
        _assert_cap(application, 32)
        status, raw = _invoke(
            application,
            "POST",
            "/strategies",
            body=b"{}",
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", b"2"),
                (b"content-length", b"2"),
            ],
        )
        assert status == 400, raw
        assert json.loads(raw)["error"]["code"] == "invalid_request"

        monkeypatch.delenv("STRATEGIES_MAX_BODY_BYTES")
        _assert_cap(application, 65_536)


def test_corrupt_ir_json_is_503(tmp_path: Path) -> None:
    """Kills S10. Unreadable canonical IR is 503, not a traceback."""
    bad_id = "ef" * 16
    with _client(tmp_path / "strategies.sqlite", raise_server_exceptions=False) as (
        client,
        store,
        _app,
    ):
        assert store._conn is not None
        store._conn.execute(
            """
            INSERT INTO strategies (
                strategy_id, user_id, name, ir_strategy_id,
                created_at, updated_at, archived_at
            ) VALUES (?, ?, 'Broken', 'broken', ?, ?, NULL)
            """,
            (bad_id, LOCAL_USER_ID, "2026-01-01T00:00:02Z", "2026-01-01T00:00:02Z"),
        )
        store._conn.execute(
            """
            INSERT INTO strategy_versions (
                strategy_id, version, user_id, dsl, ir_json, ir_hash, created_at
            ) VALUES (?, 1, ?, 'dsl', 'NOT-JSON', ?, ?)
            """,
            (bad_id, LOCAL_USER_ID, "cd" * 32, "2026-01-01T00:00:02Z"),
        )
        response = client.get(f"/strategies/{bad_id}")
        _error(response, 503, "store_unavailable")
        assert "NOT-JSON" not in response.text
        assert "Expecting value" not in response.text


def test_version_rows_reject_update_and_delete(tmp_path: Path) -> None:
    """Version rows cannot be updated or deleted, including by raw SQL."""
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        assert created.status_code == 201, created.text
        assert store._conn is not None
        with pytest.raises(sqlite3.IntegrityError) as updated:
            store._conn.execute("UPDATE strategy_versions SET dsl = 'tampered'")
        assert "strategy_versions rows are immutable" in str(updated.value)
        with pytest.raises(sqlite3.IntegrityError) as deleted:
            store._conn.execute("DELETE FROM strategy_versions")
        assert "strategy_versions rows are immutable" in str(deleted.value)
        row = store._conn.execute("SELECT dsl, COUNT(*) FROM strategy_versions").fetchone()
        assert row is not None
        assert row[0] == dsl
        assert row[1] == 1


def _strategy_count(store: StrategyStore) -> int:
    assert store._conn is not None
    return int(store._conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0])


def _assert_cap(application: Any, cap: int) -> None:
    exact = b"{" + b"x" * (cap - 2) + b"}"
    over = exact + b"x"
    assert len(exact) == cap
    assert len(over) == cap + 1
    for payload, limited in ((exact, False), (over, True)):
        status, raw = _invoke(application, "POST", "/strategies", body=payload)
        _expect_cap(status, raw, limited)
        status, raw = _invoke(
            application,
            "POST",
            "/strategies",
            headers=[(b"content-type", b"application/json")],
            receive=_one_chunk(payload),
        )
        _expect_cap(status, raw, limited)


def _one_chunk(payload: bytes) -> Any:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return receive


def _expect_cap(status: int, raw: bytes, limited: bool) -> None:
    assert len(raw) < 500
    assert b"xx" not in raw
    body = json.loads(raw)
    if limited:
        assert status == 413
        assert body["error"]["code"] == "payload_too_large"
        return
    assert status == 422
    assert body["error"]["code"] == "invalid_json"


def test_validate_reports_positions_and_does_not_write(tmp_path: Path) -> None:
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        assert created.status_code == 201, created.text
        strategy_id = created.json()["strategy_id"]
        before = _rows(store.path, "SELECT version, dsl, ir_hash FROM strategy_versions")

        checked = client.post(f"/strategies/{strategy_id}/validate", json={"dsl": dsl})
        assert checked.status_code == 200, checked.text
        body = checked.json()
        assert body["valid"] is True
        assert body["strategy_id"] == strategy_id
        assert body["name"] == created.json()["name"]
        assert body["ir_hash"] == created.json()["version"]["ir_hash"]
        assert "dsl" not in body
        assert "ir" not in body

        syntax = client.post(
            f"/strategies/{strategy_id}/validate",
            json={"dsl": "not a strategy\n"},
        )
        error = _error(syntax, 422, "syntax")
        diagnostic = error["diagnostics"][0]
        assert diagnostic["line"] == 1
        assert isinstance(diagnostic["column"], int)
        assert diagnostic["column"] >= 1
        assert "message" in diagnostic
        assert _rows(store.path, "SELECT version, dsl, ir_hash FROM strategy_versions") == before

        client.delete(f"/strategies/{strategy_id}")
        archived = client.post(f"/strategies/{strategy_id}/validate", json={"dsl": dsl})
        _error(archived, 404, "not_found")
        assert _rows(store.path, "SELECT version, dsl, ir_hash FROM strategy_versions") == before


def test_stale_save_conflicts_and_does_not_overwrite(tmp_path: Path) -> None:
    dsl, _document = _ema()
    edited = _warmup(dsl, 30)
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        assert created.status_code == 201, created.text
        strategy_id = created.json()["strategy_id"]
        head_hash = created.json()["version"]["ir_hash"]
        assert created.json()["latest_version"] == 1

        missing_hash = client.put(
            f"/strategies/{strategy_id}",
            json={"dsl": edited, "expected_version": 1},
        )
        _error(missing_hash, 422, "invalid_request")
        bad_hash = client.put(
            f"/strategies/{strategy_id}",
            json={"dsl": edited, "expected_version": 1, "expected_ir_hash": "AB" * 32},
        )
        _error(bad_hash, 422, "invalid_request")
        assert _rows(store.path, "SELECT COUNT(*) FROM strategy_versions") == [(1,)]

        saved = client.put(
            f"/strategies/{strategy_id}",
            json={"dsl": edited, "expected_version": 1, "expected_ir_hash": head_hash},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["latest_version"] == 2
        next_hash = saved.json()["version"]["ir_hash"]
        assert next_hash != head_hash

        stale = client.put(
            f"/strategies/{strategy_id}",
            json={"dsl": _warmup(dsl, 40), "expected_version": 1, "expected_ir_hash": head_hash},
        )
        error = _error(stale, 409, "version_conflict")
        assert error["current_version"] == 2
        assert error["current_ir_hash"] == next_hash
        assert error["path"] == "$.expected_version"
        wrong_hash = client.put(
            f"/strategies/{strategy_id}",
            json={"dsl": _warmup(dsl, 40), "expected_version": 2, "expected_ir_hash": head_hash},
        )
        _error(wrong_hash, 409, "version_conflict")
        assert _rows(store.path, "SELECT version FROM strategy_versions ORDER BY version") == [
            (1,),
            (2,),
        ]
        assert client.get(f"/strategies/{strategy_id}").json()["version"]["ir_hash"] == next_hash

        legacy = client.put(f"/strategies/{strategy_id}", json={"dsl": _warmup(dsl, 40)})
        assert legacy.status_code == 200, legacy.text
        assert legacy.json()["latest_version"] == 3


def test_validate_stays_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsl, _document = _ema()
    entered = threading.Event()
    release = threading.Event()
    original = routes.compile_body
    calls = {"n": 0}

    def blocking(raw: bytes) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return original(raw)
        entered.set()
        assert release.wait(3)
        return original(raw)

    monkeypatch.setattr(routes, "compile_body", blocking)
    with _client(tmp_path / "strategies.sqlite") as (client, _store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        holder: dict[str, Any] = {}

        def post() -> None:
            holder["response"] = client.post(
                f"/strategies/{strategy_id}/validate",
                json={"dsl": dsl},
            )

        worker = threading.Thread(target=post)
        worker.start()
        assert entered.wait(2)
        health: dict[str, Any] = {}

        def get_health() -> None:
            started = time.perf_counter()
            health["response"] = client.get("/health")
            health["elapsed"] = time.perf_counter() - started

        try:
            checker = threading.Thread(target=get_health)
            checker.start()
            checker.join(0.3)
            blocked = checker.is_alive()
            release.set()
            checker.join(2)
            worker.join(2)
        finally:
            release.set()
        assert blocked is False
        assert health["response"].status_code == 200
        assert health["response"].json() == {"status": "ok"}
        assert health["elapsed"] < 0.3
        assert holder["response"].status_code == 200
