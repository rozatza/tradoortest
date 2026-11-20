"""Environment settings for the author agent.

API keys are read here and nowhere else. Callers must not log the return
value, put it in an error, or write it to SQLite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.agent.errors import AgentError

TIERS = ("fast", "balanced", "max")
PROVIDERS = ("stub", "openai_compatible", "anthropic")
DEFAULT_TIER = "balanced"
DEFAULT_DB = Path("data/agent.sqlite")
DEFAULT_BODY_BYTES = 65_536
MAX_BODY_BYTES = 1_048_576
DEFAULT_BUSY_MS = 5_000
DEFAULT_MAX_STEPS = 8
HARD_MAX_STEPS = 16
DEFAULT_MAX_TOOL_CALLS = 16
HARD_MAX_TOOL_CALLS = 32
MAX_MESSAGE_CHARS = 8_000
MAX_TOOL_RESULT_CHARS = 12_000
MAX_STORED_CHARS = 24_000
MAX_PROVIDER_BYTES = 1_048_576
DEFAULT_MAX_TOKENS = 1_024
_KEY_ENV = ("AGENT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY")
_OPENAI_BASE = "https://api.openai.com/v1"
_ANTHROPIC_BASE = "https://api.anthropic.com"


@dataclass(frozen=True, slots=True)
class ResolvedTier:
    """The provider and model a session tier will call."""

    tier: str
    provider: str
    model: str
    base_url: str


def db_path() -> Path:
    """``AGENT_DB_PATH``, or ``data/agent.sqlite``."""
    return Path(_text_env("AGENT_DB_PATH", default=str(DEFAULT_DB), limit=4096))


def busy_timeout_ms() -> int:
    """``AGENT_BUSY_TIMEOUT_MS``, or 5000. Range 0 to 60000."""
    return _int_env("AGENT_BUSY_TIMEOUT_MS", default=DEFAULT_BUSY_MS, low=0, high=60_000)


def body_limit() -> int:
    """``AGENT_MAX_BODY_BYTES``, or 65536. Range 1 to 1048576."""
    return _int_env("AGENT_MAX_BODY_BYTES", default=DEFAULT_BODY_BYTES, low=1, high=MAX_BODY_BYTES)


def default_tier() -> str:
    """``AGENT_DEFAULT_TIER``, or ``balanced``."""
    raw = os.environ.get("AGENT_DEFAULT_TIER")
    if raw is None or raw.strip() == "":
        return DEFAULT_TIER
    text = raw.strip()
    if text not in TIERS:
        raise AgentError("service_unavailable", "the agent is not configured", status=503)
    return text


def resolve_tier(tier: str) -> ResolvedTier:
    """Map ``tier`` to a provider, model, and base URL."""
    if tier not in TIERS:
        raise AgentError("validation_error", "tier is not valid", path="$.tier")
    provider = _provider_name(tier)
    model = _model_name(tier)
    return ResolvedTier(
        tier=tier,
        provider=provider,
        model=model,
        base_url=_base_url(provider, tier),
    )


def api_key_for(provider: str) -> str | None:
    """Return the env key for ``provider``, or None when it is unset.

    A newline or a non-ASCII value is treated as unset so it cannot be
    placed in a header. The value is not included in any error.
    """
    names = ["AGENT_API_KEY"]
    if provider == "anthropic":
        names.append("ANTHROPIC_API_KEY")
    elif provider == "openai_compatible":
        names.extend(("OPENAI_API_KEY", "XAI_API_KEY"))
    for name in names:
        text = _secret(os.environ.get(name))
        if text is not None:
            return text
    return None


def secret_values() -> tuple[str, ...]:
    """Configured key strings, for redaction. Do not log this."""
    found: list[str] = []
    for name in _KEY_ENV:
        text = _secret(os.environ.get(name))
        if text is not None and text not in found:
            found.append(text)
    return tuple(found)


def _provider_name(tier: str) -> str:
    specific = os.environ.get(f"AGENT_PROVIDER_{tier.upper()}")
    shared = os.environ.get("AGENT_PROVIDER")
    raw = specific if specific is not None and specific.strip() != "" else shared
    if raw is None or raw.strip() == "":
        return "stub"
    name = raw.strip()
    if name not in PROVIDERS:
        raise AgentError(
            "provider_not_configured", "the model provider is not configured", status=503
        )
    return name


def _model_name(tier: str) -> str:
    raw = os.environ.get(f"AGENT_MODEL_{tier.upper()}")
    if raw is None or raw.strip() == "":
        return tier
    text = raw.strip()
    if len(text) > 128 or not text.isascii() or any(char.isspace() for char in text):
        raise AgentError("service_unavailable", "the agent is not configured", status=503)
    return text


def _base_url(provider: str, tier: str) -> str:
    if provider == "stub":
        return ""
    specific = os.environ.get(f"AGENT_BASE_URL_{tier.upper()}")
    shared = os.environ.get("AGENT_BASE_URL")
    raw = specific if specific is not None and specific.strip() != "" else shared
    if raw is None or raw.strip() == "":
        if provider == "anthropic":
            return _ANTHROPIC_BASE
        return _OPENAI_BASE
    return _clean_base(raw)


def _clean_base(raw: str) -> str:
    text = raw.strip().rstrip("/")
    if len(text) > 200 or not text.isascii() or any(char in text for char in "\x00\r\n "):
        raise AgentError(
            "provider_not_configured", "the model provider is not configured", status=503
        )
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise AgentError(
            "provider_not_configured", "the model provider is not configured", status=503
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentError(
            "provider_not_configured", "the model provider is not configured", status=503
        )
    return text


def _text_env(name: str, *, default: str, limit: int) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip()
    if len(text) > limit or "\x00" in text:
        raise AgentError("service_unavailable", "the agent is not configured", status=503)
    return text


def _int_env(name: str, *, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip()
    if not text.isascii() or not text.isdigit() or (len(text) > 1 and text.startswith("0")):
        raise AgentError("service_unavailable", "the agent is not configured", status=503)
    if len(text) > 8:
        raise AgentError("service_unavailable", "the agent is not configured", status=503)
    value = int(text)
    if value < low or value > high:
        raise AgentError("service_unavailable", "the agent is not configured", status=503)
    return value


def _secret(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if text == "" or len(text) > 512 or not text.isascii():
        return None
    if any(char in text for char in "\x00\r\n"):
        return None
    return text
