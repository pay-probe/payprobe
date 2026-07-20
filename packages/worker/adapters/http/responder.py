"""HttpResponder — a rules-driven REST/HTTP gateway simulator.

The HTTP counterpart of :class:`~worker.adapters.tcp.responder.TcpResponder`:
instead of listening on a socket and answering framed ISO 8583 / host-command
messages, it listens for **HTTP requests** and answers with templated JSON — so
PayProbe can stand in for a REST payment gateway during loopback testing with no
real upstream.

It deliberately mirrors the ``TcpResponder`` public surface (``start``/``stop``/
``serve_forever``/``port``/``protocol``/``rules``/``received``/``faults``/
``stats``/``peers``/``reset_stats``/``global_chaos``/``_label``), so it is
drop-in compatible with the orchestrator's saved-simulator registry, the Live
Sessions view and the portal Simulators page — the same way
:class:`~worker.adapters.hsm.payshield.PayShieldSimulator` is for the TCP side.

Behaviour is data-driven: a list of **rules** matches an incoming request (by
method, path and/or body fields) and dictates the reply (status, JSON body,
headers, latency, drop). The first matching rule wins; otherwise a configurable
default applies. Subclasses (e.g.
:class:`~worker.adapters.scheme.cybersource.CyberSourceSimulator`) override
:meth:`_resolve` to compute gateway-specific replies while inheriting all the
lifecycle, metrics and chaos machinery.

Config (JSON-friendly)::

    {
      "protocol": "http",
      "host": "0.0.0.0", "port": 8080,
      "rules": [
        {"when": {"method": "POST", "path": {"prefix": "/payments"},
                  "body": {"amount": {"gte": 1000}}},
         "respond": {"status": 402, "json": {"decision": "DECLINE"}}},
        {"when": {"method": "GET", "path": {"eq": "/health"}},
         "respond": {"status": 200, "json": {"status": "ok"}}}
      ],
      "default": {"status": 200, "json": {"status": "ok"}}
    }

Chaos / fault injection reuses :mod:`worker.adapters.tcp.chaos`. A top-level
``chaos`` block applies to every reply; a ``chaos`` block inside a rule's
``respond`` overrides it for matching requests. For HTTP the outcomes map to:
*latency* -> the reply is delayed; *drop* -> a 504 with the connection forced
closed (bucketed as ``(drop)``); *malformed* -> a syntactically broken JSON
body; *partial* -> a truncated body with the connection closed mid-message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiohttp import web

from ..tcp.chaos import ChaosEngine

log = logging.getLogger(__name__)


def _match_condition(value: Any, cond: Any) -> bool:
    """Does a request value satisfy a rule condition? Mirrors the TCP
    responder's matcher but tolerates numeric (JSON) values by comparing on a
    string view for the textual operators and numerically for gte/lte."""
    sval = "" if value is None else str(value)
    if isinstance(cond, dict):
        if "eq" in cond and sval != str(cond["eq"]):
            return False
        if "prefix" in cond and not sval.startswith(str(cond["prefix"])):
            return False
        if "contains" in cond and str(cond["contains"]) not in sval:
            return False
        if "in" in cond and sval not in [str(x) for x in cond["in"]]:
            return False
        if "regex" in cond:
            import re

            if not re.search(str(cond["regex"]), sval):
                return False
        if "gte" in cond and not _num_ge(value, cond["gte"]):
            return False
        if "lte" in cond and not _num_le(value, cond["lte"]):
            return False
        return True
    return sval == str(cond)


def _num_ge(value: Any, bound: Any) -> bool:
    try:
        return float(value) >= float(bound)
    except (TypeError, ValueError):
        return str(value) >= str(bound)


def _num_le(value: Any, bound: Any) -> bool:
    try:
        return float(value) <= float(bound)
    except (TypeError, ValueError):
        return str(value) <= str(bound)


def _dig(body: Any, path: str) -> Any:
    """Resolve a dotted ``a.b.c`` path into a parsed JSON body (dicts/lists)."""
    cur = body
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


class HttpResponder:
    PROTOCOL = "http"

    def __init__(self, config: dict) -> None:
        self.config = config
        self.protocol = config.get("protocol", self.PROTOCOL)
        self.rules = config.get("rules", [])
        self.default = config.get("default", {"status": 200, "json": {"status": "ok"}})

        # chaos / fault injection — a global block applies to every reply; a
        # rule's respond.chaos overrides it for matched requests.
        self.global_chaos: dict = config.get("chaos") or {}
        self.chaos = ChaosEngine(self.global_chaos.get("seed"))
        self.faults = 0

        self.received: list[dict] = []

        # aiohttp low-level server plumbing
        self._runner: web.ServerRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None
        self._closed: asyncio.Event = asyncio.Event()

        #: per-remote-host liveness for the Live Sessions view (HTTP is
        #: request/response, so a "session" is aggregated per client IP).
        self._conn_meta: dict[str, dict] = {}

        # -- live metrics (kept key-compatible with TcpResponder.stats) -------
        self.started_at = time.time()
        #: request volume keyed by ``"METHOD path"`` (the HTTP analog of by-MTI).
        self.by_mti: dict[str, int] = {}
        #: reply volume keyed by HTTP status (the analog of by-response-code).
        self.by_rc: dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> int:
        """Begin listening. Returns the bound port (useful when port=0)."""
        host = self.config.get("host", "127.0.0.1")
        port = int(self.config.get("port", 0))
        self._closed = asyncio.Event()
        self._runner = web.ServerRunner(web.Server(self._handle))
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        # resolve the actually-bound port (handles port=0)
        server = getattr(self._site, "_server", None)
        if server is not None and server.sockets:
            self._port = server.sockets[0].getsockname()[1]
        else:
            self._port = port
        return self._port

    @property
    def port(self) -> int | None:
        return self._port

    async def stop(self) -> None:
        self._closed.set()
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:  # noqa: BLE001 — best-effort teardown
                log.debug("http responder: error on cleanup: %s", exc)
            self._runner = None
            self._site = None
        self._port = None
        self._conn_meta.clear()

    async def serve_forever(self) -> None:
        """Block until :meth:`stop` is called (the server runs in the loop)."""
        await self._closed.wait()

    def set_chaos(self, cfg: dict | None) -> dict:
        """Swap the live global chaos block at runtime (mirrors
        :meth:`TcpResponder.set_chaos` — same duck-typed control surface).
        Takes effect on the next reply; empty/None turns chaos off; a ``seed``
        restarts the RNG stream for reproducibility."""
        cfg = dict(cfg or {})
        if cfg.get("seed") is not None:
            self.chaos = ChaosEngine(cfg.get("seed"))
        self.global_chaos = cfg
        self.config["chaos"] = cfg
        return cfg

    # -- metrics -------------------------------------------------------------

    def reset_stats(self) -> None:
        self.received.clear()
        self.faults = 0
        self.by_mti.clear()
        self.by_rc.clear()
        self.started_at = time.time()

    def peers(self) -> list[dict]:
        now = time.time()
        rows: list[dict] = []
        for meta in self._conn_meta.values():
            since = meta.get("since", now)
            last = meta.get("last", since)
            rows.append(
                {
                    "peer": meta.get("peer") or "?",
                    "since": round(since, 3),
                    "connected_s": round(now - since, 1),
                    "messages": meta.get("msgs", 0),
                    "idle_s": round(now - last, 1),
                }
            )
        rows.sort(key=lambda r: r["since"])
        return rows

    def stats(self) -> dict:
        elapsed = max(1e-6, time.time() - self.started_at)
        total = len(self.received)
        return {
            "received": total,
            "faults": self.faults,
            "elapsed_s": round(elapsed, 1),
            "avg_rps": round(total / elapsed, 2),
            "by_mti": dict(self.by_mti),
            "by_response_code": dict(self.by_rc),
        }

    def _response_code(self, action: dict) -> str:
        """The status this reply will carry — drives the by-response-code
        breakdown. Subclasses may override (e.g. to bucket by gateway decision)."""
        return str(action.get("status", 200))

    # -- request handling ----------------------------------------------------

    async def _handle(self, request: web.BaseRequest) -> web.StreamResponse:
        peer = request.remote or "?"
        meta = self._conn_meta.setdefault(
            peer, {"peer": peer, "since": time.time(), "msgs": 0, "last": time.time()}
        )
        meta["msgs"] += 1
        meta["last"] = time.time()

        parsed = await self._decode(request)
        self.received.append(parsed)
        mkey = f"{parsed.get('method', '?')} {parsed.get('path', '?')}"
        self.by_mti[mkey] = self.by_mti.get(mkey, 0) + 1

        action = await self._resolve_async(parsed)

        # roll chaos: merge the global block with this rule's override
        merged = {**self.global_chaos, **(action.get("chaos") or {})}
        outcome = self.chaos.plan(merged)
        if outcome.active:
            self.faults += 1
            parsed["chaos"] = outcome.summary()

        # latency: legacy delay_ms plus any chaos latency/jitter
        delay = action.get("delay_ms", 0) + outcome.latency_ms
        if delay:
            await asyncio.sleep(delay / 1000)

        # drop: no usable reply — 504 with the connection forced closed.
        if action.get("drop") or outcome.drop:
            self.by_rc["(drop)"] = self.by_rc.get("(drop)", 0) + 1
            resp = web.Response(status=504, headers={"Connection": "close"})
            resp.force_close()
            return resp

        status = int(action.get("status", 200))
        self.by_rc[str(status)] = self.by_rc.get(str(status), 0) + 1
        headers = {"Content-Type": "application/json", **(action.get("headers") or {})}
        body_obj = action.get("json", action.get("body", {}))
        text = body_obj if isinstance(body_obj, str) else json.dumps(body_obj)

        if outcome.malformed:
            # syntactically broken JSON so the client's parse fails.
            text = (text or "{}")[:-1] + ',"<<malformed>>' if len(text) > 1 else "{"

        if outcome.partial:
            # send only the first half, then hang up mid-message.
            raw = text.encode("utf-8")
            n = max(1, len(raw) // 2)
            resp = web.Response(body=raw[:n], status=status, headers=headers)
            resp.force_close()
            return resp

        return web.Response(text=text, status=status, headers=headers)

    # -- decode --------------------------------------------------------------

    async def _decode(self, request: web.BaseRequest) -> dict:
        """Parse an inbound request into the dict ``_resolve`` matches against."""
        raw = await request.read()
        body: Any = None
        text = raw.decode("utf-8", errors="replace") if raw else ""
        if text:
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text
        return {
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": body,
            "raw": text,
        }

    # -- rule resolution -----------------------------------------------------

    async def _resolve_async(self, parsed: dict) -> dict:
        """Async hook used by ``_handle``. The base responder is rules-driven and
        synchronous; subclasses override this to compute replies asynchronously."""
        return self._resolve(parsed)

    def _resolve(self, parsed: dict) -> dict:
        for rule in self.rules:
            if self._matches(rule.get("when", {}), parsed):
                return rule.get("respond", {})
        return dict(self.default)

    def _matches(self, when: dict, parsed: dict) -> bool:
        if "method" in when and not _match_condition(parsed.get("method", ""), when["method"]):
            return False
        if "path" in when and not _match_condition(parsed.get("path", ""), when["path"]):
            return False
        body = parsed.get("body")
        for key, cond in (when.get("body") or {}).items():
            if not _match_condition(_dig(body, key), cond):
                return False
        for key, cond in (when.get("query") or {}).items():
            if not _match_condition((parsed.get("query") or {}).get(key), cond):
                return False
        for key, cond in (when.get("headers") or {}).items():
            if not _match_condition((parsed.get("headers") or {}).get(key.lower()), cond):
                return False
        return True
