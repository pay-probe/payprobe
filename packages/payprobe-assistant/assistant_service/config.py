"""LLM provider configuration for the assistant (the egress boundary).

Two sources, in precedence order:

1. **Environment** (prod override / container-native):
   * ``ASSIST_LLM_PROVIDER``  — ``openai`` (default) or ``anthropic``
   * ``ASSIST_LLM_API_KEY``   — key (falls back to ``OPENAI_API_KEY`` /
     ``ANTHROPIC_API_KEY`` by provider)
   * ``ASSIST_LLM_MODEL``     — model id (provider default if unset)
   * ``ASSIST_LLM_BASE_URL``  — override the provider endpoint (optional)
2. **Settings → AI assistant** (portal-managed): when no env key is set, the
   config saved in scenario-service is pulled over the service-gated
   ``/assist/config/material`` endpoint (service JWT; users only ever see the
   masked view). Cached for a short TTL so Settings edits apply without a
   restart. Disable with ``ASSIST_SETTINGS_LLM=0`` (tests / air-gapped).

NOTE: :func:`resolve_llm` may make a short blocking HTTP call — async callers
should wrap it in ``run_in_threadpool``.
"""
from __future__ import annotations

import os
import time

_DEFAULTS = {
    "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "anthropic": ("https://api.anthropic.com/v1/messages", "claude-3-5-sonnet-latest"),
}

#: Settings-config cache: re-fetch at most every TTL seconds (also caches
#: failures, so an unreachable scenario-service can't hammer every request).
_SETTINGS_TTL = 15.0
_settings_cache: dict = {"at": 0.0, "llm": None}


def _settings_enabled() -> bool:
    return (os.environ.get("ASSIST_SETTINGS_LLM") or "1").lower() not in (
        "0", "false", "no")


def _settings_llm() -> dict | None:
    """The Settings-stored LLM config from scenario-service, or None.

    Best-effort: any failure (older scenario-service without the endpoint,
    auth, network) returns None so behavior degrades to env-only exactly as
    before. The result — including a miss — is cached for ``_SETTINGS_TTL``.
    """
    if not _settings_enabled():
        return None
    now = time.monotonic()
    if now - _settings_cache["at"] < _SETTINGS_TTL:
        return _settings_cache["llm"]
    llm: dict | None = None
    try:
        from . import rest
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
    provider = (os.environ.get("ASSIST_LLM_PROVIDER") or "openai").lower()
    d_url, d_model = _DEFAULTS.get(provider, _DEFAULTS["openai"])
    env_key = (
        os.environ.get("ASSIST_LLM_API_KEY")
        or (os.environ.get("ANTHROPIC_API_KEY") if provider == "anthropic"
            else os.environ.get("OPENAI_API_KEY"))
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
