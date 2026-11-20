"""Review guards for the strategy save pair, validate, and surrogate keys."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from tests.test_strategies import _client, _ema, _error, _rows, _warmup


def _count(path: Path, strategy_id: str) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM strategy_versions WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _put(
    client: TestClient,
    strategy_id: str,
    body: dict[str, Any],
    header: str | None,
) -> Any:
    headers = {"content-type": "application/json"}
    if header is not None:
        headers["x-tradoor-client"] = header
    return client.put(
        f"/strategies/{strategy_id}",
        content=json.dumps(body),
        headers=headers,
    )


def test_bad_pairs_are_422_and_write_nothing(tmp_path: Path) -> None:
    dsl, _document = _ema()
    edited = _warmup(dsl, 31)
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        assert created.status_code == 201, created.text
        strategy_id = created.json()["strategy_id"]
        version = created.json()["latest_version"]
        digest = created.json()["version"]["ir_hash"]
        bad = [
            {"expected_version": True, "expected_ir_hash": digest},
            {"expected_version": 0, "expected_ir_hash": digest},
            {"expected_version": -3, "expected_ir_hash": digest},
            {"expected_version": 1_000_001, "expected_ir_hash": digest},
            {"expected_version": 1.0, "expected_ir_hash": digest},
            {"expected_version": "1", "expected_ir_hash": digest},
            {"expected_version": 1, "expected_ir_hash": digest.upper()},
            {"expected_version": 1, "expected_ir_hash": digest + "0"},
            {"expected_version": 1, "expected_ir_hash": digest[:-1]},
            {"expected_version": 1, "expected_ir_hash": digest + "\n"},
            {"expected_version": 1, "expected_ir_hash": "z" * 64},
            {"expected_version": 1, "expected_ir_hash": 5},
            {"expected_version": 1, "expected_ir_hash": None},
            {"expected_version": None, "expected_ir_hash": digest},
        ]
        for pair in bad:
            for header in ("web", None):
                response = _put(client, strategy_id, {"dsl": edited, **pair}, header)
                error = _error(response, 422, "invalid_request")
                assert error["message"]
                assert _count(store.path, strategy_id) == 1
        saved = _put(
            client,
            strategy_id,
            {"dsl": edited, "expected_version": version, "expected_ir_hash": digest},
            "web",
        )
        assert saved.status_code == 200, saved.text
        assert _count(store.path, strategy_id) == 2


def test_only_the_exact_web_header_requires_the_pair(tmp_path: Path) -> None:
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        blocked = _put(client, strategy_id, {"dsl": _warmup(dsl, 41)}, "web")
        _error(blocked, 422, "invalid_request")
        assert _count(store.path, strategy_id) == 1
        for header in ("Web", "WEB", "webx", "web2"):
            response = _put(client, strategy_id, {"dsl": _warmup(dsl, 42)}, header)
            assert response.status_code == 200, (header, response.text)
        assert _rows(store.path, "SELECT version FROM strategy_versions ORDER BY version") == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
        ]


def test_validate_hides_another_owner_the_same_way_as_a_missing_id(tmp_path: Path) -> None:
    dsl, document = _ema()
    other = "cd" * 16
    ghost = "ee" * 16
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        assert created.status_code == 201, created.text
        assert store._conn is not None
        store._conn.execute(
            """
            INSERT INTO strategies (
                strategy_id, user_id, name, ir_strategy_id,
                created_at, updated_at, archived_at
            ) VALUES (?, 'other-user', 'other', 'other_sol', ?, ?, NULL)
            """,
            (other, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        bodies = (
            {"dsl": "garbage"},
            {"dsl": _warmup(dsl, 5)},
            {"ir": {"x": 1}},
            {},
        )
        for body in bodies:
            hidden = client.post(f"/strategies/{other}/validate", json=body)
            missing = client.post(f"/strategies/{ghost}/validate", json=body)
            assert hidden.status_code == 404
            assert hidden.content == missing.content
        assert document["strategy"]["id"]


def test_validate_body_cap_rejects_an_oversized_document(tmp_path: Path) -> None:
    dsl, _document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, _store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        response = client.post(
            f"/strategies/{strategy_id}/validate",
            content=json.dumps({"dsl": "a" * 70_000}),
            headers={"content-type": "application/json"},
        )
        _error(response, 413, "payload_too_large")


def test_validate_does_not_write_a_valid_or_invalid_body(tmp_path: Path) -> None:
    dsl, document = _ema()
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        before = _rows(store.path, "SELECT COUNT(*), MAX(version) FROM strategy_versions")
        for body in ({"dsl": _warmup(dsl, 7)}, {"dsl": "garbage"}, {"ir": document}):
            client.post(f"/strategies/{strategy_id}/validate", json=body)
        assert _rows(store.path, "SELECT COUNT(*), MAX(version) FROM strategy_versions") == before


def test_lone_surrogate_keys_stay_422_on_agent_and_strategy_routes(tmp_path: Path) -> None:
    dsl, _document = _ema()
    headers = {"content-type": "application/json", "x-tradoor-client": "web"}
    bodies = (
        b'{"\\ud800":1}',
        b'{"a":{"\\udfff":1}}',
        b'{"a":[{"b":{"\\ud83d":1}}]}',
        b'{"title":"t","\\ud800":1}',
        b'{"content":"hi","\\ud800":1}',
    )
    session_id = "ab" * 16
    proposal_id = "0" * 32
    with _client(tmp_path / "strategies.sqlite") as (client, store, _app):
        created = client.post("/strategies", json={"dsl": dsl})
        strategy_id = created.json()["strategy_id"]
        before = _count(store.path, strategy_id)
        routes = (
            ("POST", "/agent/sessions"),
            ("POST", f"/agent/sessions/{session_id}/messages"),
            ("POST", f"/agent/sessions/{session_id}/messages/stream"),
            ("POST", f"/agent/proposals/{proposal_id}/accept"),
            ("POST", f"/agent/proposals/{proposal_id}/reject"),
            ("PUT", f"/strategies/{strategy_id}"),
            ("POST", f"/strategies/{strategy_id}/validate"),
            ("POST", "/strategies"),
        )
        for method, path in routes:
            for body in bodies:
                response = client.request(method, path, content=body, headers=headers)
                assert response.status_code == 422, (method, path, response.status_code)
                assert "Internal Server Error" not in response.text
                payload = response.json()
                assert payload["error"]["code"]
                response.text.encode("utf-8")
                json.dumps(payload).encode("utf-8")
                assert "\\ud800" not in json.dumps(payload)
                assert "\ud800" not in response.text
        assert _count(store.path, strategy_id) == before


def test_lone_surrogate_string_values_are_422(tmp_path: Path) -> None:
    headers = {"content-type": "application/json"}
    with _client(tmp_path / "strategies.sqlite") as (client, _store, _app):
        for body in (b'{"content":"\\ud800"}', b'{"title":"\\udfff"}'):
            titled = client.post("/agent/sessions", content=body, headers=headers)
            assert titled.status_code == 422, titled.text
            titled.json()
            posted = client.post(
                f"/agent/sessions/{'ab' * 16}/messages",
                content=body,
                headers=headers,
            )
            assert posted.status_code == 422, posted.text
            posted.json()
