"""HTTP and SSE surface for the author agent."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.providers import ProviderRequest, ProviderResponse, StubProvider, ToolCall
from app.agent.store import AgentStore, ProposalRecord
from app.auth import LOCAL_USER_ID, CurrentUser, get_current_user
from app.dsl.format import format_strategy
from app.ir.validate import REPO_ROOT
from app.jobs.queue import JobQueue
from app.main import create_app
from app.strategies.compile import compile_body
from app.strategies.store import StrategyStore
from app.tools.dispatch import Dispatcher
from app.tools.runtime import ToolRuntime

_EMA = json.loads((REPO_ROOT / "schemas/examples/ema-cross.json").read_text(encoding="utf-8"))
_DSL = format_strategy(_EMA)
_OTHER = "other-user"
_LEAKS = ("traceback", "sqlite", "operationalerror", "/tmp/", "/home/", "secret", "AssertionError")


def _changed(warmup: int = 30) -> str:
    return _DSL.replace("warmup_bars: 26", f"warmup_bars: {warmup}", 1)


def _reply(text: str = "Done.", calls: tuple[ToolCall, ...] = ()) -> ProviderResponse:
    return ProviderResponse(text, calls, "stop" if not calls else "tool_use", "stub", "balanced")


def _no_leak(raw: str) -> None:
    lowered = raw.lower()
    for token in _LEAKS:
        assert token not in lowered


class _Bundle:
    def __init__(self, tmp_path: Path, provider: StubProvider | None = None) -> None:
        self.path = tmp_path / "agent.sqlite"
        self.strategy_path = tmp_path / "strategies.sqlite"
        self.job_path = tmp_path / "jobs.sqlite"
        self.agent = AgentStore(self.path, clock=lambda: "2026-01-01T00:00:00Z")
        self.strategies = StrategyStore(self.strategy_path, clock=lambda: "2026-01-01T00:00:00Z")
        self.jobs = JobQueue(self.job_path, clock=lambda: 1_700_000_000.0)
        self.provider = provider or StubProvider([_reply("Done.")])
        self.app = create_app(
            strategies=self.strategies,
            jobs=self.jobs,
            agent_store=self.agent,
            llm_provider=self.provider,
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def close(self) -> None:
        self.agent.close()
        self.strategies.close()
        self.jobs.close()

    def session(self, **body: object) -> httpx.Response:
        return self.client.post("/agent/sessions", json=body)


def _seed_other(store: AgentStore) -> tuple[str, str]:
    session = store.create_session(_OTHER, title="hidden", tier="fast", strategy_id=None)
    proposal = ProposalRecord(
        proposal_id="ab" * 16,
        session_id=session.session_id,
        turn_id="cd" * 16,
        user_id=_OTHER,
        strategy_id="ef" * 16,
        from_version=1,
        to_version=2,
        from_ir_hash="12" * 32,
        to_ir_hash="34" * 32,
        dsl_diff="hidden",
        ir_diff=[],
        candidate_dsl="hidden dsl",
        candidate_name="hidden",
        candidate_ir_strategy_id="hidden",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
        decided_at=None,
        applied_version=None,
        applied_ir_hash=None,
    )
    store.add_proposal(proposal)
    return session.session_id, proposal.proposal_id


def _save(bundle: _Bundle, dsl: str = _DSL) -> str:
    compiled = compile_body(json.dumps({"dsl": dsl}).encode("utf-8"))
    record, _version = bundle.strategies.create(LOCAL_USER_ID, compiled)
    return record.strategy_id


def _propose(bundle: _Bundle, strategy_id: str, dsl: str) -> str:
    bundle.provider._script = [
        _reply(
            "",
            (
                ToolCall(
                    "c1",
                    "update_strategy",
                    {
                        "strategy_id": strategy_id,
                        "dsl": dsl,
                        "mode": "apply",
                        "confirm": True,
                        "expected_version": 1,
                    },
                ),
            ),
        ),
        _reply("proposed"),
    ]
    bundle.provider._index = 0
    created = bundle.session(title="edit")
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    posted = bundle.client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"content": "please edit"},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["proposals"]
    return posted.json()["proposals"][0]["proposal_id"]


def test_sessions_messages_and_events_ignore_spoofed_owner(tmp_path: Path) -> None:
    bundle = _Bundle(tmp_path)
    try:
        created = bundle.client.post(
            "/agent/sessions",
            json={"title": "one", "tier": "fast"},
            headers={"X-User-Id": _OTHER},
            params={"user_id": _OTHER, "owner_id": _OTHER},
        )
        assert created.status_code == 201
        assert created.headers["location"].endswith(
            f"/agent/sessions/{created.json()['session_id']}"
        )
        assert created.json()["user_id"] == LOCAL_USER_ID
        assert created.json()["tier"] == "fast"
        listed = bundle.client.get("/agent/sessions", params={"user_id": _OTHER})
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        session_id = created.json()["session_id"]
        posted = bundle.client.post(
            f"/agent/sessions/{session_id}/messages",
            json={"content": "hello", "max_steps": 16, "max_tool_calls": 1},
            params={"owner_id": _OTHER},
        )
        assert posted.status_code == 200
        body = posted.json()
        assert body["status"] == "completed"
        assert body["assistant_message"] == "Done."
        assert body["provider"] == "stub"
        assert [event["type"] for event in body["events"]] == [
            "user_message",
            "assistant_message",
            "turn_complete",
        ]
        assert body["events"][0]["seq"] == 1
        detail = bundle.client.get(f"/agent/sessions/{session_id}", params={"user_id": _OTHER})
        assert detail.status_code == 200
        assert detail.json()["messages"][0]["role"] == "user"
        replay = bundle.client.get(f"/agent/sessions/{session_id}/events", params={"after_seq": 1})
        assert [event["seq"] for event in replay.json()["events"]] == [2, 3]
        unknown = bundle.client.post(
            "/agent/sessions",
            json={"user_id": _OTHER, "title": "nope"},
        )
        assert unknown.status_code == 422
        assert unknown.json()["error"]["path"] == "$.user_id"
        bad_tier = bundle.client.post("/agent/sessions", json={"tier": "huge"})
        assert bad_tier.status_code == 422
        assert bad_tier.json()["error"]["path"] == "$.tier"
        assert bundle.client.get("/agent/sessions/not-an-id").status_code == 422
        missing = bundle.client.get(f"/agent/sessions/{'ab' * 16}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
    finally:
        bundle.close()


def test_other_users_rows_are_not_found_and_a_foreign_user_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _Bundle(tmp_path)
    try:
        session_id, proposal_id = _seed_other(bundle.agent)
        compiled = compile_body(json.dumps({"dsl": _DSL}).encode("utf-8"))
        hidden, _version = bundle.strategies.create(_OTHER, compiled)
        bundle.jobs.start(
            user_id=_OTHER,
            request_key="other-job",
            body={"ir": compiled.ir, "bars": {"sol": [{"open_time": 1, "close": 1}]}},
        )
        for path in (
            f"/agent/sessions/{session_id}",
            f"/agent/sessions/{session_id}/events",
            f"/agent/proposals/{proposal_id}",
        ):
            response = bundle.client.get(path)
            assert response.status_code == 404, path
            assert response.json()["error"]["code"] == "not_found"
            assert "hidden" not in response.text
        posted = bundle.client.post(
            f"/agent/sessions/{session_id}/messages",
            json={"content": "hello"},
        )
        assert posted.status_code == 404
        listed = bundle.client.get("/agent/proposals", params={"session_id": session_id})
        assert listed.status_code == 404
        own = bundle.client.get("/agent/proposals")
        assert own.status_code == 200
        assert own.json()["proposals"] == []
        for action in ("accept", "reject"):
            decided = bundle.client.post(f"/agent/proposals/{proposal_id}/{action}")
            assert decided.status_code == 404
        assert bundle.strategies.get(_OTHER, hidden.strategy_id)[0].latest_version == 1
    finally:
        bundle.close()

    monkeypatch.chdir(tmp_path)
    application = create_app()
    application.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=_OTHER)
    client = TestClient(application)
    denied = client.post("/agent/sessions", json={"title": "no"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    assert application.state.agent_store is None
    assert not (tmp_path / "data" / "agent.sqlite").exists()


def test_accept_is_once_and_reject_does_not_write(tmp_path: Path) -> None:
    bundle = _Bundle(tmp_path)
    try:
        strategy_id = _save(bundle)
        proposal_id = _propose(bundle, strategy_id, _changed(30))
        assert bundle.strategies.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 1
        rejected = bundle.client.post(f"/agent/proposals/{proposal_id}/reject")
        assert rejected.status_code == 200
        assert rejected.json()["proposal"]["status"] == "rejected"
        assert rejected.json()["event"]["type"] == "proposal_decided"
        assert bundle.strategies.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 1
        again = bundle.client.post(f"/agent/proposals/{proposal_id}/reject")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "proposal_already_decided"
        accepted_id = _propose(bundle, strategy_id, _changed(30))
        first = bundle.client.post(f"/agent/proposals/{accepted_id}/accept")
        assert first.status_code == 200, first.text
        assert first.json()["strategy"]["version"] == 2
        assert first.json()["proposal"]["status"] == "accepted"
        second = bundle.client.post(f"/agent/proposals/{accepted_id}/accept")
        assert second.status_code == 409
        assert bundle.strategies.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 2
        stale_id = _propose(bundle, strategy_id, _changed(40))
        dispatcher = Dispatcher(ToolRuntime(strategies=bundle.strategies, jobs=bundle.jobs))
        outside = dispatcher.invoke(
            "update_strategy",
            {
                "strategy_id": strategy_id,
                "dsl": _changed(50),
                "mode": "apply",
                "confirm": True,
                "expected_version": 2,
            },
            CurrentUser(user_id=LOCAL_USER_ID),
        )
        assert outside["ok"] is True
        conflict = bundle.client.post(f"/agent/proposals/{stale_id}/accept")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "version_conflict"
        assert bundle.strategies.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 3
        stored = bundle.strategies.get_version(LOCAL_USER_ID, strategy_id, 3)
        assert "warmup_bars: 50" in stored.dsl
        assert bundle.client.post(f"/agent/proposals/{stale_id}/accept").status_code == 409
        assert bundle.client.post(f"/agent/proposals/{stale_id}/reject").status_code == 409
        row = bundle.agent.get_proposal(LOCAL_USER_ID, stale_id)
        assert row is not None and row.status == "conflict"
    finally:
        bundle.close()


def test_two_accepts_apply_one_version(tmp_path: Path) -> None:
    bundle = _Bundle(tmp_path)
    try:
        strategy_id = _save(bundle)
        proposal_id = _propose(bundle, strategy_id, _changed(30))

        async def both() -> tuple[httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=bundle.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await asyncio.gather(
                    client.post(f"/agent/proposals/{proposal_id}/accept"),
                    client.post(f"/agent/proposals/{proposal_id}/accept"),
                )

        left, right = asyncio.run(both())
        codes = sorted([left.status_code, right.status_code])
        assert codes == [200, 409]
        assert bundle.strategies.get(LOCAL_USER_ID, strategy_id)[0].latest_version == 2
    finally:
        bundle.close()


def test_stream_replays_ordered_events(tmp_path: Path) -> None:
    provider = StubProvider(
        [
            _reply("", (ToolCall("c1", "read_authoring_reference", {"topic": "overview"}),)),
            _reply("finished"),
        ]
    )
    bundle = _Bundle(tmp_path, provider)
    try:
        session_id = bundle.session(title="stream").json()["session_id"]
        response = bundle.client.post(
            f"/agent/sessions/{session_id}/messages/stream",
            json={"content": "read the overview"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        blocks = [block for block in response.text.split("\n\n") if block.strip()]
        events = []
        for block in blocks:
            lines = block.split("\n")
            assert lines[0].startswith("id: ")
            assert lines[1].startswith("event: ")
            assert lines[2].startswith("data: ")
            payload = json.loads(lines[2].removeprefix("data: "))
            assert lines[0].removeprefix("id: ") == str(payload["seq"])
            assert lines[1].removeprefix("event: ") == payload["type"]
            events.append(payload)
        assert [event["type"] for event in events] == [
            "user_message",
            "tool_start",
            "tool_complete",
            "assistant_message",
            "turn_complete",
        ]
        assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]
        replay = bundle.client.get(f"/agent/sessions/{session_id}/events", params={"after_seq": 3})
        assert [event["seq"] for event in replay.json()["events"]] == [4, 5]
    finally:
        bundle.close()


def test_health_stays_up_during_a_blocked_turn_and_stream(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked(request: ProviderRequest) -> ProviderResponse:
        entered.set()
        release.wait(3)
        return _reply("Done.")

    bundle = _Bundle(tmp_path, StubProvider([blocked]))
    try:
        session_id = bundle.session().json()["session_id"]

        async def sync_probe() -> None:
            transport = httpx.ASGITransport(app=bundle.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                posted = asyncio.create_task(
                    client.post(
                        f"/agent/sessions/{session_id}/messages",
                        json={"content": "wait"},
                    )
                )
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                started = time.monotonic()
                health = await client.get("/health")
                elapsed = time.monotonic() - started
                release.set()
                saved = await posted
            assert entered.is_set()
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert elapsed < 0.3
            assert saved.status_code == 200

        asyncio.run(sync_probe())
        entered.clear()
        release.clear()
        bundle.provider._index = 0

        async def stream_probe() -> None:
            transport = httpx.ASGITransport(app=bundle.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                async with client.stream(
                    "POST",
                    f"/agent/sessions/{session_id}/messages/stream",
                    json={"content": "wait again"},
                ) as response:
                    assert response.status_code == 200
                    buffer = ""
                    chunks = response.aiter_text()
                    async for chunk in chunks:
                        buffer += chunk
                        if "user_message" in buffer:
                            break
                    started = time.monotonic()
                    health = await client.get("/health")
                    elapsed = time.monotonic() - started
                    release.set()
                    async for chunk in chunks:
                        buffer += chunk
            assert health.status_code == 200
            assert elapsed < 0.3
            assert "turn_complete" in buffer

        asyncio.run(stream_probe())
    finally:
        release.set()
        bundle.close()


def test_body_cap_rejects_declared_and_chunked_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_MAX_BODY_BYTES", "32")
    application = create_app(llm_provider=StubProvider([_reply()]))
    unread = {"called": False}

    async def skip() -> dict[str, Any]:
        unread["called"] = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    status, raw = _asgi(
        application,
        "/agent/sessions",
        headers=[(b"content-type", b"application/json"), (b"content-length", b"999999")],
        receive=skip,
    )
    assert status == 413
    assert unread["called"] is False
    assert json.loads(raw)["error"]["code"] == "payload_too_large"
    state = {"calls": 0}

    async def chunks() -> dict[str, Any]:
        state["calls"] += 1
        if state["calls"] == 1:
            return {"type": "http.request", "body": b"x" * 20, "more_body": True}
        if state["calls"] == 2:
            return {"type": "http.request", "body": b"y" * 20, "more_body": True}
        raise AssertionError("the tail of a capped body was read")

    status, raw = _asgi(
        application,
        "/agent/sessions/not-an-id/messages/stream",
        headers=[(b"content-type", b"application/json")],
        receive=chunks,
    )
    assert status == 422
    assert json.loads(raw)["error"]["code"] == "invalid_id"
    session = TestClient(application).post("/agent/sessions", json={"title": "cap"})
    assert session.status_code == 201
    target = f"/agent/sessions/{session.json()['session_id']}/messages/stream"
    status, raw = _asgi(
        application,
        target,
        headers=[(b"content-type", b"application/json")],
        receive=chunks,
    )
    assert status == 413
    assert state["calls"] == 2
    assert json.loads(raw)["error"]["code"] == "payload_too_large"
    monkeypatch.setenv("AGENT_MAX_BODY_BYTES", "65536")
    client = TestClient(application)
    nan = client.post(
        "/agent/sessions",
        content=b'{"title":NaN}',
        headers={"content-type": "application/json"},
    )
    assert nan.status_code == 422
    assert nan.json()["error"]["code"] == "invalid_json"
    session_id = session.json()["session_id"]
    boolean = client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"content": "hi", "max_steps": True},
    )
    assert boolean.status_code == 422
    assert boolean.json()["error"]["path"] == "$.max_steps"
    huge = client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"content": "hi", "max_steps": 17},
    )
    assert huge.status_code == 422
    long = client.post(
        f"/agent/sessions/{session_id}/messages",
        json={"content": "x" * 8001},
    )
    assert long.status_code == 422
    assert long.json()["error"]["path"] == "$.content"


def test_locked_corrupt_and_assertion_failures_stay_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "agent.sqlite"
    store = AgentStore(path, busy_timeout_ms=0, clock=lambda: "2026-01-01T00:00:00Z")
    application = create_app(agent_store=store, llm_provider=StubProvider([_reply()]))
    client = TestClient(application, raise_server_exceptions=False)
    other = sqlite3.connect(path, isolation_level=None, timeout=0)
    other.execute("BEGIN EXCLUSIVE")
    try:
        locked = client.post("/agent/sessions", json={"title": "locked"})
        assert locked.status_code == 503
        assert locked.json()["error"]["code"] == "service_unavailable"
        _no_leak(locked.text)
        assert "locked" not in locked.text.lower()
        assert client.get("/health").json() == {"status": "ok"}
    finally:
        other.execute("ROLLBACK")
        other.close()
    recovered = client.post("/agent/sessions", json={"title": "open"})
    assert recovered.status_code == 201
    store.close()

    class Boom:
        name = "stub"

        def complete(self, _request: ProviderRequest) -> ProviderResponse:
            raise AssertionError("secret /tmp/agent.sqlite redacted-test-token-value")

    live = AgentStore(tmp_path / "live.sqlite", clock=lambda: "2026-01-01T00:00:00Z")
    application = create_app(agent_store=live, llm_provider=Boom())
    client = TestClient(application, raise_server_exceptions=False)
    session_id = client.post("/agent/sessions", json={}).json()["session_id"]
    blown = client.post(f"/agent/sessions/{session_id}/messages", json={"content": "boom"})
    assert blown.status_code == 500
    assert blown.json()["error"]["code"] == "internal_error"
    assert blown.json()["error"]["message"] == "the request could not be completed"
    assert "Internal Server Error" not in blown.text
    _no_leak(blown.text)
    assert "redacted-test-token-value" not in blown.text
    live.close()

    monkeypatch.chdir(tmp_path)
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a database")
    monkeypatch.setenv("AGENT_DB_PATH", str(corrupt))
    application = create_app()
    client = TestClient(application, raise_server_exceptions=False)
    failed = client.post("/agent/sessions", json={})
    assert failed.status_code == 503
    _no_leak(failed.text)
    assert "not a database" not in failed.text
    assert application.state.agent_store is None
    folder = tmp_path / "not-a-file.sqlite"
    folder.mkdir()
    monkeypatch.setenv("AGENT_DB_PATH", str(folder))
    blocked = client.post("/agent/sessions", json={})
    assert blocked.status_code == 503
    assert application.state.agent_store is None
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "ok.sqlite"))
    opened = client.post("/agent/sessions", json={"title": "recovered"})
    assert opened.status_code == 201, opened.text
    assert isinstance(application.state.agent_store, AgentStore)
    application.state.agent_store.close()


def test_bad_body_limit_is_a_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_MAX_BODY_BYTES", "nope")
    application = create_app(llm_provider=StubProvider([_reply()]))
    client = TestClient(application, raise_server_exceptions=False)
    response = client.post("/agent/sessions", json={"title": "cap"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert response.json()["error"]["message"] == "the agent is not configured"
    assert "Internal Server Error" not in response.text
    _no_leak(response.text)
    assert "nope" not in response.text


def test_stream_persists_a_worker_error(tmp_path: Path) -> None:
    class Boom:
        name = "stub"

        def complete(self, _request: ProviderRequest) -> ProviderResponse:
            raise AssertionError("secret /tmp/agent.sqlite redacted-test-token-value")

    store = AgentStore(tmp_path / "agent.sqlite", clock=lambda: "2026-01-01T00:00:00Z")
    application = create_app(agent_store=store, llm_provider=Boom())
    client = TestClient(application, raise_server_exceptions=False)
    try:
        session_id = client.post("/agent/sessions", json={}).json()["session_id"]
        response = client.post(
            f"/agent/sessions/{session_id}/messages/stream",
            json={"content": "boom"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        for block in (part for part in response.text.split("\n\n") if part.strip()):
            lines = block.split("\n")
            payload = json.loads(lines[2].removeprefix("data: "))
            assert lines[0] == f"id: {payload['seq']}"
            events.append(payload)
        assert [event["type"] for event in events] == ["user_message", "error", "turn_complete"]
        assert [event["seq"] for event in events] == [1, 2, 3]
        assert events[1]["payload"]["code"] == "internal_error"
        assert events[2]["payload"]["status"] == "error"
        assert events[2]["payload"]["error"]["code"] == "internal_error"
        _no_leak(response.text)
        assert "redacted-test-token-value" not in response.text
        replay = client.get(f"/agent/sessions/{session_id}/events")
        assert [event["seq"] for event in replay.json()["events"]] == [1, 2, 3]
        assert [event["type"] for event in replay.json()["events"]] == [
            "user_message",
            "error",
            "turn_complete",
        ]
    finally:
        store.close()


def test_disconnect_during_a_slow_turn_keeps_health_fast(tmp_path: Path) -> None:
    """Cancel the stream the way a server does when the client drops it.

    A synchronous ``thread.join`` in the generator runs on the event loop.
    ``/health`` has to answer while that cleanup is in progress.
    """
    entered = threading.Event()
    release = threading.Event()

    def blocked(_request: ProviderRequest) -> ProviderResponse:
        entered.set()
        release.wait(8)
        return _reply("Done.")

    bundle = _Bundle(tmp_path, StubProvider([blocked]))
    try:
        session_id = bundle.session().json()["session_id"]
        path = f"/agent/sessions/{session_id}/messages/stream"

        async def probe() -> float:
            first_body = asyncio.Event()

            async def receive() -> dict[str, Any]:
                return {
                    "type": "http.request",
                    "body": b'{"content":"wait"}',
                    "more_body": False,
                }

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.body" and message.get("body"):
                    first_body.set()

            scope = _scope("POST", path, [(b"content-type", b"application/json")])
            app_task = asyncio.create_task(bundle.app(scope, receive, send))
            await asyncio.wait_for(first_body.wait(), timeout=3)
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            app_task.cancel()
            started = time.monotonic()
            health_task = asyncio.create_task(_asgi_call(bundle.app, "GET", "/health"))
            done, _pending = await asyncio.wait({health_task}, timeout=0.2)
            elapsed = time.monotonic() - started
            release.set()
            if health_task not in done:
                health_task.cancel()
            else:
                status, raw = health_task.result()
                assert status == 200
                assert json.loads(raw) == {"status": "ok"}
            try:
                await asyncio.wait_for(app_task, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
            return elapsed

        assert asyncio.run(probe()) < 0.2
    finally:
        release.set()
        bundle.close()


def _scope(
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "root_path": "",
    }


async def _asgi_call(app: Any, method: str, path: str) -> tuple[int, bytes]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(_scope(method, path, []), receive, send)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, raw


def _asgi(
    app: Any,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]],
    receive: Any,
) -> tuple[int, bytes]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, raw
