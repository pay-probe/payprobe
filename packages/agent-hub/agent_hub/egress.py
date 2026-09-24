"""LLM egress allowlist (ADR-0010 phase 5).

Every prompt an agent sends carries platform data (tool results, run
summaries, connection names). The provider URL comes from configuration that
an admin can edit in the portal (Settings → AI assistant, `base_url`), so a
stolen admin session could point the platform's LLM traffic at any host. This
module is the fence: :func:`check` refuses a URL unless its host is allowed
and its scheme is https (plain http only for hosts named explicitly by the
operator in the environment, for local proxies).

Allowed by default: the official provider hosts, plus the host of
``ASSIST_LLM_BASE_URL`` when set (environment is operator-controlled).
``AGENT_HUB_EGRESS_ALLOW`` adds hosts: a comma list of exact hostnames or
``*.suffix`` wildcards, optionally ``host:port``.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_HOSTS: frozenset[str] = frozenset({"api.openai.com", "api.anthropic.com"})


def _host_of(url: str) -> str:
    """``host`` or ``host:port``; the scheme's default port is dropped, so a
    listed host matches only on its default port unless listed with a port."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme)
    return f"{host}:{port}" if port and port != default else host


def explicit_hosts() -> frozenset[str]:
    """Hosts the operator named in the environment (may use http)."""
    out: set[str] = set()
    for raw in (os.environ.get("AGENT_HUB_EGRESS_ALLOW") or "").split(","):
        item = raw.strip().lower()
        if item:
            out.add(item)
    base = os.environ.get("ASSIST_LLM_BASE_URL") or ""
    if base:
        out.add(_host_of(base))
    return frozenset(out)


def allowed_hosts() -> frozenset[str]:
    return DEFAULT_HOSTS | explicit_hosts()


def _matches(host: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host.endswith(suffix) and host != suffix
    return host == pattern


def check(url: str) -> str | None:
    """Reason the URL may not be called, or None when it is allowed."""
    parts = urlsplit(url)
    host = _host_of(url)
    if not host:
        return f"egress refused: no host in {url!r}"
    if "@" in (parts.netloc or ""):
        return f"egress refused: userinfo in URL ({parts.netloc})"
    if not any(_matches(host, p) for p in allowed_hosts()):
        return (
            f"egress refused: host '{host}' is not in the LLM egress allowlist "
            "(AGENT_HUB_EGRESS_ALLOW)"
        )
    if parts.scheme != "https" and not any(_matches(host, p) for p in explicit_hosts()):
        # plain http only where the operator named the host (a local proxy
        # such as Ollama on localhost:11434); the default hosts are https-only
        return f"egress refused: {parts.scheme}:// to '{host}' (https required)"
    return None


__all__ = ["DEFAULT_HOSTS", "allowed_hosts", "check", "explicit_hosts"]
