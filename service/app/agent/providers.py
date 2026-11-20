"""Swappable model providers.

``StubProvider`` is deterministic and does not open a socket. The HTTP
adapters speak OpenAI-compatible chat completions and Anthropic messages
with ``httpx``. Keys stay in the request header and never appear in an
error, a log line, or a returned body.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.agent.config import DEFAULT_MAX_TOKENS, MAX_PROVIDER_BYTES, api_key_for, resolve_tier
from app.agent.errors import AgentError
from app.agent.sanitize import redact_secrets

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One model tool call. ``arguments`` is a dict or a raw JSON string."""

    call_id: str
    name: str
    arguments: object


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn of model context. Tool results use ``role="tool"``."""

    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """What one completion call sends. ``tools`` are ECMA-262 JSON Schemas."""

    system_prompt: str
    messages: tuple[ChatMessage, ...]
    tools: tuple[Mapping[str, Any], ...]
    tier: str
    model: str
    max_tokens: int = DEFAULT_MAX_TOKENS


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Text and tool calls from one completion. ``usage`` is token counts only."""

    assistant_text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    provider: str
    model: str
    usage: Mapping[str, int] | None = None


ScriptStep = Callable[[ProviderRequest], ProviderResponse] | ProviderResponse


class LLMProvider(Protocol):
    """A model adapter. ``complete`` is synchronous and runs off the event loop."""

    name: str

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return the next assistant step."""


class StubProvider:
    """Scripted replies for tests and for a process with no model configured."""

    name = "stub"

    def __init__(self, script: list[ScriptStep] | None = None) -> None:
        self._script = list(script or [])
        self._index = 0
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        item = self._next()
        if item is None:
            produced = ProviderResponse(
                assistant_text="Done.",
                tool_calls=(),
                stop_reason="stop",
                provider=self.name,
                model=request.model,
            )
        elif callable(item):
            produced = item(request)
        else:
            produced = item
        if not isinstance(produced, ProviderResponse):
            raise AgentError("internal_error", "the model provider is unavailable", status=503)
        return ProviderResponse(
            assistant_text=produced.assistant_text,
            tool_calls=produced.tool_calls,
            stop_reason=produced.stop_reason,
            provider=self.name,
            model=request.model,
            usage=produced.usage,
        )

    def _next(self) -> ScriptStep | None:
        if not self._script:
            return None
        if self._index < len(self._script):
            item = self._script[self._index]
            self._index += 1
            return item
        last = self._script[-1]
        if callable(last):
            return last
        return None


class OpenAICompatibleProvider:
    """``POST {base}/chat/completions`` with a bearer token from the environment."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        payload = _post_json(
            self._client,
            self._api_key,
            f"{self._base_url}/chat/completions",
            {"authorization": f"Bearer {self._api_key}", "content-type": "application/json"},
            _openai_body(request),
        )
        return _openai_response(payload, request)


class AnthropicProvider:
    """``POST {base}/v1/messages`` with ``x-api-key`` from the environment."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        payload = _post_json(
            self._client,
            self._api_key,
            f"{self._base_url}/v1/messages",
            {
                "x-api-key": str(self._api_key),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            _anthropic_body(request),
        )
        return _anthropic_response(payload, request)


def build_provider(
    resolved_provider: str, base_url: str, *, client: httpx.Client | None = None
) -> LLMProvider:
    """Construct the adapter named by config. A missing key is still constructed.

    ``complete`` raises ``provider_not_configured`` when the key is absent,
    and it does so before any HTTP call.
    """
    if resolved_provider == "stub":
        return StubProvider()
    key = api_key_for(resolved_provider)
    if resolved_provider == "anthropic":
        return AnthropicProvider(api_key=key, base_url=base_url, client=client)
    if resolved_provider == "openai_compatible":
        return OpenAICompatibleProvider(api_key=key, base_url=base_url, client=client)
    raise AgentError("provider_not_configured", "the model provider is not configured", status=503)


def provider_for(
    tier: str,
    injected: LLMProvider | None,
) -> tuple[LLMProvider, str, str]:
    """Return the adapter, its public name, and the model for ``tier``."""
    resolved = resolve_tier(tier)
    if injected is not None:
        name = getattr(injected, "name", None)
        public = (
            name
            if isinstance(name, str) and name in {"stub", "openai_compatible", "anthropic"}
            else "stub"
        )
        return injected, public, resolved.model
    if resolved.provider != "stub" and api_key_for(resolved.provider) is None:
        raise AgentError(
            "provider_not_configured",
            "the model provider is not configured",
            status=503,
        )
    adapter = build_provider(resolved.provider, resolved.base_url)
    return adapter, resolved.provider, resolved.model


def _post_json(
    client: httpx.Client | None,
    api_key: str | None,
    url: str,
    headers: dict[str, str],
    body: Mapping[str, Any],
) -> dict[str, Any]:
    if api_key is None:
        raise AgentError(
            "provider_not_configured", "the model provider is not configured", status=503
        )
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )
    owns = client is None
    http = client if client is not None else httpx.Client(timeout=_TIMEOUT)
    try:
        try:
            response = http.post(url, headers=headers, content=encoded)
        except httpx.TimeoutException:
            raise AgentError(
                "service_unavailable", "the model provider timed out", status=503
            ) from None
        except httpx.HTTPError:
            raise AgentError(
                "service_unavailable",
                "the model provider is unavailable",
                status=503,
            ) from None
    finally:
        if owns:
            http.close()
    return _read_provider(response)


def _read_provider(response: httpx.Response) -> dict[str, Any]:
    if response.status_code == 429:
        raise AgentError("rate_limited", "the model provider is rate limited", status=429)
    if response.status_code >= 400:
        raise AgentError("service_unavailable", "the model provider is unavailable", status=503)
    raw = response.content
    if len(raw) > MAX_PROVIDER_BYTES:
        raise AgentError("service_unavailable", "the model provider is unavailable", status=503)
    try:
        payload = json.loads(redact_secrets(raw.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeError):
        raise AgentError(
            "service_unavailable", "the model provider is unavailable", status=503
        ) from None
    if not isinstance(payload, dict):
        raise AgentError("service_unavailable", "the model provider is unavailable", status=503)
    return payload


def _openai_body(request: ProviderRequest) -> dict[str, Any]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in request.tools
    ]
    return {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": _openai_messages(request),
        "tools": tools,
        "tool_choice": "auto",
    }


def _openai_messages(request: ProviderRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": request.system_prompt}]
    for message in request.messages:
        if message.role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or None,
                    "tool_calls": [_openai_call(call) for call in message.tool_calls],
                }
            )
            continue
        messages.append({"role": message.role, "content": message.content})
    return messages


def _openai_call(call: ToolCall) -> dict[str, Any]:
    if isinstance(call.arguments, str):
        arguments = call.arguments
    else:
        arguments = json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))
    return {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.name, "arguments": arguments},
    }


def _openai_response(payload: Mapping[str, Any], request: ProviderRequest) -> ProviderResponse:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AgentError("service_unavailable", "the model provider is unavailable", status=503)
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AgentError("service_unavailable", "the model provider is unavailable", status=503)
    text = message.get("content")
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = ""
    calls: list[ToolCall] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for index, item in enumerate(raw_calls[:64]):
            parsed = _openai_tool_call(item, index)
            if parsed is not None:
                calls.append(parsed)
    finish = choices[0].get("finish_reason")
    if calls:
        stop = "tool_use"
    elif finish == "length":
        stop = "length"
    else:
        stop = "stop"
    return ProviderResponse(
        assistant_text=text,
        tool_calls=tuple(calls),
        stop_reason=stop,
        provider="openai_compatible",
        model=request.model,
        usage=_usage(payload, "prompt_tokens", "completion_tokens"),
    )


def _openai_tool_call(item: object, index: int) -> ToolCall | None:
    if not isinstance(item, dict):
        return None
    function = item.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    return ToolCall(
        call_id=_call_id(item.get("id"), index),
        name=name[:64],
        arguments=function.get("arguments", "{}"),
    )


def _anthropic_body(request: ProviderRequest) -> dict[str, Any]:
    return {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": request.system_prompt,
        "messages": _anthropic_messages(request.messages),
        "tools": [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["parameters"],
            }
            for tool in request.tools
        ],
    }


def _anthropic_messages(messages: tuple[ChatMessage, ...]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "user":
            converted.append({"role": "user", "content": message.content})
            continue
        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                raw = call.arguments if isinstance(call.arguments, dict) else {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": raw,
                    }
                )
            converted.append(
                {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}
            )
            continue
        block = {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id or "",
            "content": message.content,
        }
        if (
            converted
            and converted[-1]["role"] == "user"
            and isinstance(converted[-1]["content"], list)
        ):
            converted[-1]["content"].append(block)
        else:
            converted.append({"role": "user", "content": [block]})
    return converted


def _anthropic_response(payload: Mapping[str, Any], request: ProviderRequest) -> ProviderResponse:
    content = payload.get("content")
    if not isinstance(content, list):
        raise AgentError("service_unavailable", "the model provider is unavailable", status=503)
    texts: list[str] = []
    calls: list[ToolCall] = []
    for index, block in enumerate(content[:64]):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "tool_use" and isinstance(block.get("name"), str):
            calls.append(
                ToolCall(
                    call_id=_call_id(block.get("id"), index),
                    name=str(block["name"])[:64],
                    arguments=block.get("input") if isinstance(block.get("input"), dict) else {},
                )
            )
    stop_reason = payload.get("stop_reason")
    if calls:
        stop = "tool_use"
    elif stop_reason == "max_tokens":
        stop = "length"
    else:
        stop = "stop"
    return ProviderResponse(
        assistant_text="".join(texts),
        tool_calls=tuple(calls),
        stop_reason=stop,
        provider="anthropic",
        model=request.model,
        usage=_usage(payload, "input_tokens", "output_tokens"),
    )


def _usage(payload: Mapping[str, Any], incoming: str, outgoing: str) -> dict[str, int] | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    left = raw.get(incoming)
    right = raw.get(outgoing)
    if type(left) is not int or type(right) is not int:
        return None
    if left < 0 or right < 0 or left > 10_000_000 or right > 10_000_000:
        return None
    return {"input_tokens": left, "output_tokens": right}


def _call_id(raw: object, index: int) -> str:
    if isinstance(raw, str) and 1 <= len(raw) <= 80 and raw.isascii():
        if all(char.isalnum() or char in "-_" for char in raw):
            return raw
    return f"call{index}"
