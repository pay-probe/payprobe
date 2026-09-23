"""LLM backends for the heartbeat runner (ADR-0010 phase 2).

``LLMBackend.complete(convo, tools) -> CallerResult`` is one tool-calling turn.
Two implementations:

* :class:`FakeLLMBackend` replays a script of replies, so the runner, budgets,
  scoping, journal and cancel paths are all tested without a provider (and in
  CI). The script is the fixture; the assertions are on what the runner did.
* :class:`ProviderLLMBackend` calls OpenAI or Anthropic through the shared
  ``payprobe_common.llm_provider`` code (the same code the assistant uses),
  with the config the platform already manages (D4): ``ASSIST_LLM_*`` env
  wins, otherwise Settings → AI assistant via scenario-service's service-gated
  ``/assist/config/material``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Protocol

from payprobe_common import llm_provider

from . import egress, rest

_DEFAULTS = {
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "anthropic": ("https://api.anthropic.com/v1/messages", "claude-3-5-sonnet-latest"),
}
_SETTINGS_TTL = 15.0
_settings_cache: dict = {"at": 0.0, "llm": None}


class LLMBackend(Protocol):
    def complete(self, convo: list[dict], tools: list[dict]) -> llm_provider.CallerResult: ...

    @property
    def usage(self) -> dict: ...

    @property
    def model(self) -> str: ...


class FakeLLMBackend:
    """Replays ``script`` in order; each entry is ``{"text": ..., "tool_calls":
    [{"name", "args"}]}``. When the script is exhausted it answers with
    ``final`` and no tool calls. Token usage is synthetic but monotonic so
    budget tests are deterministic (``tokens_per_turn`` per call)."""

    def __init__(
        self, script: list[dict] | None = None, *, final: str = "done", tokens_per_turn: int = 100
    ) -> None:
        self.script = list(script or [])
        self.final = final
        self.calls: list[list[dict]] = []  # every convo the runner sent
        self._usage = {"input": 0, "output": 0}
        self._tokens = tokens_per_turn

    @property
    def usage(self) -> dict:
        return dict(self._usage)

    @property
    def model(self) -> str:
        return "fake"

    def complete(self, convo: list[dict], tools: list[dict]) -> llm_provider.CallerResult:
        self.calls.append([dict(m) for m in convo])
        self._usage["input"] += self._tokens
        self._usage["output"] += self._tokens // 4
        if not self.script:
            return {"text": self.final, "tool_calls": []}
        entry = self.script.pop(0)
        calls = [
            {"id": f"call-{i}", "name": c["name"], "args": c.get("args") or {}}
            for i, c in enumerate(entry.get("tool_calls") or [])
        ]
        return {"text": entry.get("text") or "", "tool_calls": calls}


def _post_json(url: str, headers: dict, body: dict) -> dict:
    # The one place a prompt leaves agent-hub: refuse hosts outside the egress
    # allowlist before anything is sent (a refusal fails the heartbeat, which
    # alerts; it never falls back to another host).
    if reason := egress.check(url):
        raise RuntimeError(reason)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection error: {exc.reason}") from exc


def _settings_llm() -> dict | None:
    if (os.environ.get("ASSIST_SETTINGS_LLM") or "1").lower() in ("0", "false", "no"):
        return None
    now = time.monotonic()
    if now - _settings_cache["at"] < _SETTINGS_TTL:
        return _settings_cache["llm"]
    llm = None
    try:
        data = rest.request("GET", rest.s("/assist/config/material"), timeout=5)
        if data and data.get("enabled") and data.get("api_key"):
            provider = (data.get("provider") or "openai").lower()
            d_url, d_model = _DEFAULTS.get(provider, _DEFAULTS["openai"])
            llm = {
                "provider": provider,
                "enabled": True,
                "api_key": data["api_key"],
                "base_url": data.get("base_url") or d_url,
                "model": data.get("model") or d_model,
                "source": "settings",
            }
    except Exception:  # noqa: BLE001 — degrade to env-only on any failure
        llm = None
    _settings_cache.update(at=now, llm=llm)
    return llm


def resolve_llm() -> dict:
    """Same precedence as the assistant: env override wins, else Settings."""
    provider = (os.environ.get("ASSIST_LLM_PROVIDER") or "openai").lower()
    d_url, d_model = _DEFAULTS.get(provider, _DEFAULTS["openai"])
    env_key = (
        os.environ.get("ASSIST_LLM_API_KEY")
        or (
            os.environ.get("ANTHROPIC_API_KEY")
            if provider == "anthropic"
            else os.environ.get("OPENAI_API_KEY")
        )
        or ""
    )
    if not env_key:
        settings = _settings_llm()
        if settings:
            return settings
    return {
        "provider": provider,
        "enabled": bool(env_key),
        "api_key": env_key,
        "base_url": os.environ.get("ASSIST_LLM_BASE_URL") or d_url,
        "model": os.environ.get("ASSIST_LLM_MODEL") or d_model,
        "source": "env" if env_key else None,
    }


class ProviderLLMBackend:
    def __init__(self, llm: dict, *, model: str | None = None, temperature: float = 0.0) -> None:
        self.llm = dict(llm)
        if model:
            self.llm["model"] = model
        self.temperature = temperature
        self._usage = {"input": 0, "output": 0}

    @property
    def usage(self) -> dict:
        return dict(self._usage)

    @property
    def model(self) -> str:
        return str(self.llm.get("model"))

    def complete(self, convo: list[dict], tools: list[dict]) -> llm_provider.CallerResult:
        captured: dict = {}

        def post(url: str, headers: dict, body: dict) -> dict:
            data = _post_json(url, headers, body)
            captured.update(data)
            return data

        out = llm_provider.provider_tool_call(
            convo, tools, self.llm, post, temperature=self.temperature
        )
        u = llm_provider.usage_of(captured, self.llm.get("provider") or "openai")
        self._usage["input"] += u["input"]
        self._usage["output"] += u["output"]
        return out
