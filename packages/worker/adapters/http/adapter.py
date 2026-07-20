"""HttpAdapter — a generic REST/HTTP connection adapter.

Where the engine's ``http`` *node* makes a one-off request authored per step, this
is a reusable *connection*: a base URL plus named actions, callable by name from
scenarios, participant flows and groups (and overridable per environment). It
reuses the engine's :func:`~worker.engine.http_runner.run_http` so the request
building, auth, body shaping and ``${...}`` interpolation are identical to the
http node.

Config (worker-shaped, e.g. an environment ``adapters`` entry)::

    {
      "adapter": "http",
      "base_url": "https://api.example.com/v1",
      "headers": {"X-Tenant": "acme"},
      "authentication": {"type": "bearer", "token": "${vars.api_key}"},
      "actions": {
        "authorize": {"method": "POST", "path": "/payments"},
        "get_order": {"method": "GET",  "path": "/orders/${request.id}"}
      }
    }

``execute(action, payload)`` resolves the action's ``method`` + ``path`` against
``base_url``, sends ``payload`` as the JSON body (for write methods), and returns
the parsed response as a :class:`StepResult` (``response_payload`` =
``{status_code, headers, body}``).
"""

from __future__ import annotations

import time
from typing import Any

from ..base.base_adapter import BaseAdapter, StepResult


class HttpAdapter(BaseAdapter):
    async def connect(self) -> None:
        cfg = self.config
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        # Fail fast with a config error instead of letting every request die
        # later with httpx's cryptic "UnsupportedProtocol: Request URL is
        # missing an 'http://' or 'https://' protocol". An empty value here
        # usually means the variable it interpolates (e.g.
        # ${vars.restpay_base_url}) was never filled in.
        if not self.base_url.lower().startswith(("http://", "https://")):
            got = f" (got {self.base_url!r})" if self.base_url else ""
            raise ValueError(
                "http connection has no usable base_url — it must be a full "
                f"URL starting with http:// or https://{got}. Set it on the "
                "connection (or fill the variable it interpolates)."
            )
        self.actions: dict[str, dict] = cfg.get("actions") or {}
        self.headers: dict[str, Any] = cfg.get("headers") or {}
        self.auth: dict[str, Any] = cfg.get("authentication") or {}
        self.options: dict[str, Any] = cfg.get("options") or {}

    def _url(self, path: str) -> str:
        if not path:
            return self.base_url
        return self.base_url + (path if path.startswith("/") else "/" + path)

    async def execute(self, action: str, payload: dict) -> StepResult:
        from ...engine.http_runner import run_http  # lazy: avoid import cycle

        spec = self.actions.get(action) or {}
        # Unknown action: fall back to the action name as a path (so a bare
        # "GET /health" style call still works without a declared action).
        method = str(spec.get("method") or ("POST" if payload else "GET")).upper()
        path = spec.get("path", action if "/" in action else "/")
        write = method in ("POST", "PUT", "PATCH", "DELETE")
        cfg = {
            "method": method,
            "url": self._url(path),
            "sendHeaders": bool(self.headers),
            "headers": [{"name": k, "value": v} for k, v in self.headers.items()],
            "authentication": self.auth,
            "sendBody": write and payload is not None,
            "contentType": "json",
            "body": payload,
            "options": self.options,
        }
        # context lets the url/headers/body interpolate ${request.*} from payload
        context = {"request": payload, **(payload if isinstance(payload, dict) else {})}
        start = time.monotonic()
        res = await run_http(cfg, context)
        return StepResult(
            success=res.ok,
            request_payload=res.request or {"action": action, "payload": payload},
            response_payload=res.response or {},
            duration_ms=int((time.monotonic() - start) * 1000),
            error=res.error,
        )

    async def health_check(self) -> bool:
        """Best-effort reachability: a GET against the base URL. Never raises."""
        try:
            from ...engine.http_runner import run_http  # lazy: avoid import cycle

            res = await run_http({"method": "GET", "url": self.base_url}, {})
            return res.ok
        except Exception:  # noqa: BLE001
            return False

    async def disconnect(self) -> None:
        return
