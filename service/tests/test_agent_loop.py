"""Author loop, providers, guardrails, and the 24 hour persistence sanitizer."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agent.loop import TurnLimits, accept_proposal, run_turn
from app.agent.providers import (
    ChatMessage,
    OpenAICompatibleProvider,
    ProviderRequest,
    ProviderResponse,
    StubProvider,
    ToolCall,
    build_provider,
    provider_for,
)
from app.agent.store import AgentStore
from app.auth import LOCAL_USER_ID, CurrentUser
from app.dsl.format import format_strategy
from app.ir.validate import REPO_ROOT
from app.jobs.queue import JobQueue
from app.ohlcv.adapters.any_mint import AnyMintSource
from app.ohlcv.routes import OhlcvRuntime
from app.ohlcv.store import SqliteBarStore
from app.strategies.compile import compile_body
from app.strategies.store import StrategyStore
from app.tools.runtime import ToolRuntime
from test_backtest import _START, _strategy
from test_ohlcv_any_mint import MINT, POOLS

_USER = CurrentUser(user_id=LOCAL_USER_ID)
_EMA = json.loads((REPO_ROOT / "schemas/examples/ema-cross.json").read_text(encoding="utf-8"))
_DSL = format_strategy(_EMA)
_KEY = "redacted-test-api-key"
_PRICE = 481516.23
_VOLUME = 90210.77
_CLOCK = "2026-01-01T00:00:00Z"


def _changed() -> str:
    return _DSL.replace("warmup_bars: 26", "warmup_bars: 30", 1)


def _reply(text: str = "Done.", calls: tuple[ToolCall, ...] = ()) -> ProviderResponse:
    return ProviderResponse(text, calls, "stop" if not calls else "tool_use", "stub", "balanced")


def _tool(name: str, arguments: dict[str, Any], call_id: str) -> ProviderResponse:
    return _reply("", (ToolCall(call_id, name, arguments),))


def _request() -> ProviderRequest:
    return ProviderRequest(
        system_prompt="sys",
        messages=(ChatMessage("user", "hello"),),
        tools=(
            {
                "name": "validate",
                "description": "check",
                "parameters": {"type": "object", "additionalProperties": False},
            },
        ),
        tier="fast",
        model="model-fast",
    )


class _World:
    def __init__(self, tmp_path: Path, *, ohlcv: bool = False) -> None:
        self.jobs_path = tmp_path / "jobs.sqlite"
        self.agent = AgentStore(tmp_path / "agent.sqlite", clock=lambda: _CLOCK)
        self.strategies = StrategyStore(tmp_path / "strategies.sqlite", clock=lambda: _CLOCK)
        self.jobs = JobQueue(self.jobs_path, clock=lambda: 1_700_000_000.0)
        self._source: AnyMintSource | None = None
        self._bars: SqliteBarStore | None = None
        runtime_ohlcv = None
        if ohlcv:
            self.ohlcv_path = tmp_path / "ohlcv.sqlite"

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/pools"):
                    return httpx.Response(200, json=POOLS)
                return httpx.Response(404, json={"data": []})

            client = httpx.Client(transport=httpx.MockTransport(handler))
            self._source = AnyMintSource(
                client,
                min_interval=0,
                max_calls=5,
                sleep=lambda _seconds: None,
                now=lambda: 1_700_000_000,
            )
            self._bars = SqliteBarStore(self.ohlcv_path, now=lambda: 1_700_000_000)
            runtime_ohlcv = OhlcvRuntime(self._bars, self._source, lambda: 1_700_000_000)
        self.runtime = ToolRuntime(
            strategies=self.strategies,
            jobs=self.jobs,
            ohlcv=runtime_ohlcv,
        )
        self.session_id = self.agent.create_session(
            LOCAL_USER_ID,
            title="author",
            tier="balanced",
            strategy_id=None,
        ).session_id
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.agent.close()
        self.strategies.close()
        self.jobs.close()
        if self._source is not None:
            self._source.close()
        if self._bars is not None:
            self._bars.close()

    def turn(self, provider: StubProvider, content: str = "edit", **limits: int) -> dict[str, Any]:
        return run_turn(
            store=self.agent,
            runtime=self.runtime,
            provider=provider,
            user=_USER,
            session_id=self.session_id,
            content=content,
            tier="balanced",
            provider_name="stub",
            model="balanced",
            limits=TurnLimits(**limits) if limits else TurnLimits(),
        )


def _jobs(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    assert row is not None
    return int(row[0])


def _versions(store: StrategyStore, strategy_id: str) -> int:
    return store.get(LOCAL_USER_ID, strategy_id)[0].latest_version


def _sqlite_blob(path: Path) -> str:
    parts: list[bytes] = []
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            parts.append(candidate.read_bytes())
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tables = [
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for table in tables:
            if not table.replace("_", "").isalnum():
                continue
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                parts.append(repr(tuple(row)).encode("utf-8", "replace"))
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            parts.append(candidate.read_bytes())
    return b"\n".join(parts).decode("utf-8", "replace")


def test_stub_is_deterministic_and_repeats_a_trailing_callable() -> None:
    seen: list[str] = []

    def step(request: ProviderRequest) -> ProviderResponse:
        seen.append(request.model)
        return _reply("ok")

    provider = StubProvider([_reply("first"), step])
    first = provider.complete(_request())
    second = provider.complete(_request())
    third = provider.complete(_request())
    assert first.assistant_text == "first"
    assert second.assistant_text == "ok"
    assert third.assistant_text == "ok"
    assert first.provider == "stub"
    assert first.model == "model-fast"
    assert seen == ["model-fast", "model-fast"]
    assert len(provider.requests) == 3


def test_openai_and_anthropic_adapters_format_tiers_and_hide_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_API_KEY", _KEY)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": f"echo {_KEY}",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "validate",
                                            "arguments": json.dumps({"note": _KEY}),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": True},
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": f"echo {_KEY}"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "validate",
                        "input": {"ok": True},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 2, "output_tokens": 4},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    openai = OpenAICompatibleProvider(
        api_key=_KEY,
        base_url="https://example.test/v1",
        client=client,
    )
    produced = openai.complete(_request())
    sent = captured[0]
    body = json.loads(sent.content)
    assert sent.url.path == "/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {_KEY}"
    assert body["model"] == "model-fast"
    assert body["tool_choice"] == "auto"
    assert body["tools"][0]["function"]["name"] == "validate"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert produced.provider == "openai_compatible"
    assert produced.stop_reason == "tool_use"
    assert _KEY not in produced.assistant_text
    assert _KEY not in produced.tool_calls[0].arguments
    assert produced.usage is None

    monkeypatch.setenv("AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_PROVIDER_FAST", "anthropic")
    monkeypatch.setenv("AGENT_MODEL_FAST", "model-fast")
    resolved_provider, _name, model = provider_for("fast", None)
    assert model == "model-fast"
    assert resolved_provider.name == "anthropic"
    anthropic = build_provider("anthropic", "https://example.test", client=client)
    produced = anthropic.complete(_request())
    sent = captured[1]
    body = json.loads(sent.content)
    assert sent.url.path == "/v1/messages"
    assert sent.headers["x-api-key"] == _KEY
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "model-fast"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    assert produced.usage == {"input_tokens": 2, "output_tokens": 4}
    assert _KEY not in produced.assistant_text

    def fail(status: int) -> httpx.Response:
        return httpx.Response(status, json={"error": {"api_key": _KEY}})

    for status, code in ((429, "rate_limited"), (500, "service_unavailable")):
        broken = OpenAICompatibleProvider(
            api_key=_KEY,
            base_url="https://example.test/v1",
            client=httpx.Client(
                transport=httpx.MockTransport(lambda _request, status=status: fail(status))
            ),
        )
        with pytest.raises(Exception) as exc:
            broken.complete(_request())
        assert exc.value.code == code
        assert _KEY not in exc.value.message
        assert _KEY not in str(exc.value)
        assert "example.test" not in exc.value.message

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    slow = OpenAICompatibleProvider(
        api_key=_KEY,
        base_url="https://example.test/v1",
        client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    with pytest.raises(Exception) as exc:
        slow.complete(_request())
    assert exc.value.code == "service_unavailable"
    assert exc.value.message == "the model provider timed out"
    assert "TimeoutException" not in str(exc.value)

    missing = OpenAICompatibleProvider(
        api_key=None, base_url="https://example.test/v1", client=client
    )
    calls = len(captured)
    with pytest.raises(Exception) as exc:
        missing.complete(_request())
    assert exc.value.code == "provider_not_configured"
    assert len(captured) == calls
    monkeypatch.delenv("AGENT_API_KEY")
    with pytest.raises(Exception) as exc:
        provider_for("balanced", None)
    assert exc.value.code == "provider_not_configured"


def test_update_apply_from_the_model_is_a_pending_proposal(tmp_path: Path) -> None:
    world = _World(tmp_path)
    try:
        wrote = world.strategies.create(
            LOCAL_USER_ID,
            compile_body(json.dumps({"dsl": _DSL}).encode("utf-8")),
        )
        strategy_id = wrote[0].strategy_id
        version = wrote[1]
        provider = StubProvider(
            [
                _tool(
                    "update_strategy",
                    {
                        "strategy_id": strategy_id,
                        "dsl": _changed(),
                        "mode": "apply",
                        "confirm": True,
                        "expected_version": 1,
                        "expected_ir_hash": version.ir_hash,
                    },
                    "c1",
                ),
                _reply("proposed"),
            ]
        )
        result = world.turn(provider)
        update = next(
            tool for tool in provider.requests[0].tools if tool["name"] == "update_strategy"
        )
        assert "pending proposal" in update["description"]
        assert "\\Z" not in json.dumps(tuple(provider.requests[0].tools))
        assert list(result["events"][0]) == [
            "seq",
            "event_id",
            "session_id",
            "turn_id",
            "type",
            "created_at",
            "payload",
        ]
        assert result["status"] == "completed"
        assert result["proposals"][0]["status"] == "pending"
        assert "storage was not modified" in json.dumps(result["events"])
        assert _versions(world.strategies, strategy_id) == 1
        same = world.turn(
            StubProvider(
                [
                    _tool(
                        "update_strategy",
                        {
                            "strategy_id": strategy_id,
                            "dsl": version.dsl,
                            "mode": "apply",
                            "confirm": True,
                            "expected_version": 1,
                        },
                        "c2",
                    ),
                    _reply("same"),
                ]
            )
        )
        assert same["proposals"] == []
        assert _versions(world.strategies, strategy_id) == 1
        accepted = accept_proposal(
            store=world.agent,
            runtime=world.runtime,
            user=_USER,
            proposal_id=result["proposals"][0]["proposal_id"],
        )
        assert accepted["strategy"]["version"] == 2
        assert _versions(world.strategies, strategy_id) == 2
        again = world.agent.get_proposal(LOCAL_USER_ID, result["proposals"][0]["proposal_id"])
        assert again is not None and again.status == "accepted"
    finally:
        world.close()


def test_backtest_requires_validate_for_the_exact_hash(tmp_path: Path) -> None:
    world = _World(tmp_path)
    try:
        wrote = world.strategies.create(
            LOCAL_USER_ID,
            compile_body(json.dumps({"dsl": _DSL}).encode("utf-8")),
        )
        strategy_id = wrote[0].strategy_id
        bars = {
            "sol": [{"open_time": _START, "close": 100}, {"open_time": _START + 60, "close": 110}]
        }
        blocked = world.turn(
            StubProvider(
                [
                    _tool("backtest_start", {"strategy_id": strategy_id, "bars": bars}, "c1"),
                    _reply(),
                ]
            )
        )
        assert blocked["status"] == "completed"
        assert _jobs(world.jobs_path) == 0
        assert "validation_required" in json.dumps(blocked["events"])
        invalid = world.turn(
            StubProvider(
                [
                    _tool("validate", {"dsl": "not a strategy"}, "c2"),
                    _tool("backtest_start", {"dsl": _DSL, "bars": bars}, "c3"),
                    _reply(),
                ]
            )
        )
        assert _jobs(world.jobs_path) == 0
        assert "validation_required" in json.dumps(invalid["events"])
        shifted = world.turn(
            StubProvider(
                [
                    _tool("validate", {"dsl": _DSL}, "c4"),
                    _tool("backtest_start", {"dsl": _changed(), "bars": bars}, "c5"),
                    _reply(),
                ]
            )
        )
        assert _jobs(world.jobs_path) == 0
        assert "validation_required" in json.dumps(shifted["events"])
        fresh = world.turn(
            StubProvider([_tool("write_strategy", {"dsl": _changed()}, "c6"), _reply()])
        )
        assert fresh["status"] == "completed"
        second = [
            row
            for row in _owned(world.strategies)
            if row is not None and row.strategy_id != strategy_id
        ]
        assert len(second) == 1
        new_id = second[0].strategy_id
        denied = world.turn(
            StubProvider(
                [_tool("backtest_start", {"strategy_id": new_id, "bars": bars}, "c7"), _reply()]
            )
        )
        assert _jobs(world.jobs_path) == 0
        assert "validation_required" in json.dumps(denied["events"])
        allowed = world.turn(
            StubProvider(
                [
                    _tool("validate", {"dsl": _DSL}, "c8"),
                    _tool(
                        "backtest_start",
                        {"strategy_id": strategy_id, "bars": bars, "request_key": "exact"},
                        "c9",
                    ),
                    _reply(),
                ]
            )
        )
        assert allowed["status"] == "completed"
        assert _jobs(world.jobs_path) == 1
    finally:
        world.close()


def _owned(store: StrategyStore):
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute(
            "SELECT strategy_id FROM strategies WHERE user_id = ?",
            (LOCAL_USER_ID,),
        ).fetchall()
    return [store.find(LOCAL_USER_ID, str(row[0])) for row in rows]


def test_step_and_tool_caps_stop_the_turn(tmp_path: Path) -> None:
    world = _World(tmp_path)
    try:

        def always(_request: ProviderRequest) -> ProviderResponse:
            return _tool("read_authoring_reference", {"topic": "overview"}, "loop")

        limited = world.turn(StubProvider([always]), max_steps=2, max_tool_calls=16)
        assert limited["status"] == "stopped_limit"
        assert limited["error"]["code"] == "step_limit_exceeded"
        types = [event["type"] for event in limited["events"]]
        assert types[-2:] == ["error", "turn_complete"]
        burst = world.turn(
            StubProvider(
                [
                    _reply(
                        "",
                        (
                            ToolCall("a", "read_authoring_reference", {"topic": "overview"}),
                            ToolCall("b", "read_authoring_reference", {"topic": "dsl"}),
                        ),
                    ),
                    _reply("after"),
                ]
            ),
            max_steps=8,
            max_tool_calls=1,
        )
        assert burst["status"] == "stopped_limit"
        assert burst["error"]["code"] == "tool_limit_exceeded"
        assert [event["type"] for event in burst["events"]][-2:] == ["error", "turn_complete"]
    finally:
        world.close()


def test_agent_sqlite_omits_bars_results_and_pool_snapshots(tmp_path: Path) -> None:
    world = _World(tmp_path, ohlcv=True)
    try:
        bars = {
            "sol": [
                {
                    "open_time": _START,
                    "open": _PRICE,
                    "high": _PRICE,
                    "low": _PRICE,
                    "close": _PRICE,
                    "volume": _VOLUME,
                },
                {"open_time": _START + 60, "close": _PRICE, "volume": _VOLUME},
            ]
        }
        ir = _strategy()
        state = {"n": 0, "job_id": ""}

        def script(request: ProviderRequest) -> ProviderResponse:
            state["n"] += 1
            if state["n"] == 1:
                return _tool("search_asset", {"query": MINT}, "s1")
            if state["n"] == 2:
                return _tool("validate", {"ir": ir}, "s2")
            if state["n"] == 3:
                return _tool(
                    "backtest_start",
                    {"ir": ir, "bars": bars, "request_key": "ttl-check"},
                    "s3",
                )
            if state["n"] == 4:
                last = request.messages[-1].content
                state["job_id"] = json.loads(last)["data"]["job_id"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    job = world.jobs.get(LOCAL_USER_ID, state["job_id"])
                    if job.status in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                return _tool("backtest_get", {"job_id": state["job_id"]}, "s4")
            return _reply("Done.")

        result = world.turn(StubProvider([script]), "inspect")
        assert result["status"] == "completed"
        assert state["job_id"]
        assert world.jobs.get(LOCAL_USER_ID, state["job_id"]).status == "succeeded"
        world.close()
        agent_text = _sqlite_blob(tmp_path / "agent.sqlite")
        job_text = _sqlite_blob(world.jobs_path)
        pool_text = _sqlite_blob(tmp_path / "ohlcv.sqlite")
        for token in (
            "481516",
            "90210",
            "PoolHigh",
            "QuoteUsdcMint",
            "tradoortest.backtest_result.v1",
            "final_equity",
            "equity_after",
            "50.00",
        ):
            assert token not in agent_text
        assert "481516" in job_text
        assert "tradoortest.backtest_result.v1" in job_text
        assert "final_equity" in job_text
        assert "PoolHigh" in pool_text
        assert "bars_omitted" in agent_text
        assert "result_omitted" in agent_text
        assert "provider_data_omitted" in agent_text
    finally:
        world.close()
