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
        #: Body encoding for write actions: ``json`` (default) or ``form``
        #: (url-encoded with provider bracket nesting — what Stripe requires;
        #: see ADR-0009). A per-action ``content_type`` overrides this.
        self.content_type: str = str(cfg.get("content_type") or "json")

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
        # Per-action headers (ADR-0009): an action may carry headers of its
        # own — e.g. PayPal's documented ``PayPal-Mock-Response`` negative-
        # testing header on a *_expecting_decline action. They win over
        # connection-level headers of the same name, and interpolate
        # ``${request.*}`` like the url does.
        headers = {**self.headers, **(spec.get("headers") or {})}
        cfg = {
            "method": method,
            "url": self._url(path),
            "sendHeaders": bool(headers),
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "authentication": self.auth,
            "sendBody": write and payload is not None,
            "contentType": str(spec.get("content_type") or self.content_type),
            "body": payload,
            "options": self.options,
        }
        # context lets the url/headers/body interpolate ${request.*} from payload
        context = {"request": payload, **(payload if isinstance(payload, dict) else {})}
        start = time.monotonic()
        res = await run_http(cfg, context)
        # An action may declare non-2xx statuses as expected outcomes
        # (``accept_statuses: [200, 402]``): a provider *decline* is a
        # legitimate result a scenario asserts on, not a transport failure
        # (ADR-0009 — testing the decline path is half the point of a PSP
        # integration). Accepted statuses clear the error so assertions decide.
        accept = spec.get("accept_statuses")
        success = res.ok
        error = res.error
        if accept and res.status_code is not None and res.status_code in accept:
            success, error = True, None
        return StepResult(
            success=success,
            request_payload=res.request or {"action": action, "payload": payload},
            response_payload=res.response or {},
            duration_ms=int((time.monotonic() - start) * 1000),
            error=error,
        )

    async def health_check(self) -> bool:
        """Best-effort reachability: a GET against the base URL. Never raises.

        Two optional config keys tune it: ``health_path`` probes a different
        path than the API root, and ``health_any_status: true`` counts *any*
        HTTP answer as healthy — a payment provider's API root answers 401/404
        to an anonymous GET, which still proves the endpoint is up (ADR-0009:
        real Stripe/Adyen/PayPal and their simulators all behave this way).
        """
        try:
            from ...engine.http_runner import run_http  # lazy: avoid import cycle

            path = self.config.get("health_path")
            url = self._url(str(path)) if path else self.base_url
            res = await run_http({"method": "GET", "url": url}, {})
            if self.config.get("health_any_status"):
                return res.status_code is not None
            return res.ok
        except Exception:  # noqa: BLE001
            return False

    async def disconnect(self) -> None:
        return
