"""Execution of ``http`` nodes — outbound HTTP requests, n8n-style.

An ``http`` node performs a single HTTP request and exposes the response as
``${node_id.response.*}`` (``status_code``, ``headers``, ``body``). Like every
other node it can interpolate ``${...}`` references in its URL, query params,
headers and body, so a request can be built from earlier step results.

The config shape mirrors the editor panel::

    {
      "method": "GET",
      "url": "https://api.example.com/v1/txn/${step_001.response.rrn}",
      "authentication": {"type": "bearer", "token": "..."},
      "sendQueryParameters": true,  "queryParameters": [{"name", "value"}],
      "sendHeaders": true,          "headers": [{"name", "value"}],
      "sendBody": true, "contentType": "json", "body": "{ ... }",
      "options": {"timeout_ms": 30000, "followRedirects": true,
                  "ignoreSsl": false, "responseFormat": "auto"}
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .variables import resolve_value, UnresolvedReferenceError

DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 300_000


@dataclass
class HttpResult:
    ok: bool
    status_code: int | None = None
    response: dict = field(default_factory=dict)
    request: dict = field(default_factory=dict)
    error: str | None = None


def _resolve(value: Any, context: dict) -> Any:
    try:
        return resolve_value(value, context)
    except UnresolvedReferenceError as exc:
        raise ValueError(f"unresolved reference: {exc}") from exc


def _pairs(items: Any, context: dict) -> dict[str, str]:
    """Turn ``[{"name","value"}]`` rows into a resolved {name: value} dict."""
    out: dict[str, str] = {}
    for row in items or []:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        out[name] = str(_resolve(row.get("value", ""), context))
    return out


def _build_auth(auth: dict, headers: dict, params: dict, context: dict):
    """Apply an authentication config, mutating headers/params in place.

    Returns an httpx auth tuple for Basic, else ``None``.
    """
    kind = (auth or {}).get("type", "none")
    if kind == "bearer":
        token = str(_resolve(auth.get("token", ""), context))
        headers["Authorization"] = f"Bearer {token}"
    elif kind == "basic":
        user = str(_resolve(auth.get("username", ""), context))
        pwd = str(_resolve(auth.get("password", ""), context))
        return (user, pwd)
    elif kind == "header":
        name = str(auth.get("headerName", "")).strip()
        if name:
            headers[name] = str(_resolve(auth.get("headerValue", ""), context))
    elif kind == "query":
        name = str(auth.get("paramName", "")).strip()
        if name:
            params[name] = str(_resolve(auth.get("paramValue", ""), context))
    return None


def _parse_body(text: str, content_type: str | None) -> Any:
    if content_type and "json" in content_type and text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


async def run_http(config: dict, context: dict) -> HttpResult:
    """Perform the request described by ``config`` against ``context``."""
    try:
        import httpx
    except ImportError:
        return HttpResult(False, None, error="httpx is not installed in this runtime")

    cfg = config or {}
    method = str(cfg.get("method", "GET")).upper()
    options = cfg.get("options") or {}

    try:
        url = str(_resolve(cfg.get("url", ""), context)).strip()
        if not url:
            return HttpResult(False, None, error="HTTP node has no URL")

        params = (
            _pairs(cfg.get("queryParameters"), context) if cfg.get("sendQueryParameters") else {}
        )
        headers = _pairs(cfg.get("headers"), context) if cfg.get("sendHeaders") else {}
        auth = _build_auth(cfg.get("authentication") or {}, headers, params, context)

        content = data = json_body = None
        if cfg.get("sendBody"):
            content_type = cfg.get("contentType", "json")
            raw = _resolve(cfg.get("body", ""), context)
            if content_type == "json":
                if isinstance(raw, (dict, list)):
                    json_body = raw
                else:
                    text = str(raw).strip()
                    json_body = json.loads(text) if text else None
            elif content_type == "form":
                data = _pairs(cfg.get("bodyParameters"), context)
            else:  # raw
                content = str(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        return HttpResult(False, None, error=f"request build failed: {exc}")

    try:
        timeout = (
            min(max(int(options.get("timeout_ms", DEFAULT_TIMEOUT_MS)), 1), MAX_TIMEOUT_MS) / 1000
        )
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_MS / 1000
    follow = options.get("followRedirects", True)
    verify = not options.get("ignoreSsl", False)

    request_info = {"method": method, "url": url, "headers": headers}
    if json_body is not None:
        request_info["body"] = json_body
    elif data is not None:
        request_info["body"] = data
    elif content is not None:
        request_info["body"] = content

    try:
        async with httpx.AsyncClient(
            verify=verify,
            follow_redirects=follow,
            timeout=timeout,
        ) as client:
            resp = await client.request(
                method,
                url,
                params=params or None,
                headers=headers or None,
                json=json_body,
                data=data,
                content=content,
                auth=auth,
            )
    except httpx.TimeoutException:
        return HttpResult(
            False,
            None,
            request=request_info,
            error=f"request timed out after {int(timeout * 1000)} ms",
        )
    except httpx.HTTPError as exc:
        return HttpResult(False, None, request=request_info, error=f"{type(exc).__name__}: {exc}")

    response_format = options.get("responseFormat", "auto")
    if response_format == "text":
        body: Any = resp.text
    elif response_format == "json":
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = resp.text
    else:  # auto: parse by content-type
        body = _parse_body(resp.text, resp.headers.get("content-type"))

    response = {
        "status_code": resp.status_code,
        "headers": dict(resp.headers),
        "body": body,
    }
    return HttpResult(
        ok=resp.status_code < 400,
        status_code=resp.status_code,
        response=response,
        request=request_info,
        error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
    )
