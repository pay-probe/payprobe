"""GrpcAdapter — a general, config-driven gRPC adapter.

Calls any method on a gRPC service with no generated stubs: it loads a compiled
proto ``FileDescriptorSet`` and invokes methods dynamically, supporting all four
RPC shapes — **unary**, **server-streaming**, **client-streaming**, and
**bidirectional streaming**. The request/response message types and streaming
flags are derived from the descriptors, so a step just names a method and passes
a JSON-shaped payload.

``grpc`` is imported lazily (only at ``connect``) so the worker package imports
fine without grpcio installed; the registry registers this adapter behind a
try-import like the other optional adapters.

The schema (which methods exist + their message types) can come from any of three
sources, tried in this order — whichever are configured are merged together:

1. a precompiled ``FileDescriptorSet`` (``descriptor_set_path`` / ``_b64``);
2. **custom-declared** ``.proto`` source compiled on the fly
   (``proto_inline`` / ``proto_paths``);
3. **discovered** from the server via gRPC server reflection (``reflection: true``).

Config::

    {
      "target": "localhost:50051",          # or "host" + "port"

      # --- schema source: any combination of the three ---
      "descriptor_set_path": "bundle.pb",   # or "descriptor_set_b64": "..."
      "proto_inline": "syntax=\"proto3\"; ...",  # raw .proto text (str / list / {name: text})
      "proto_paths": ["svc.proto"],         # .proto files on the worker host
      "proto_include_dirs": ["proto"],      # extra -I import roots
      "reflection": true,                   # discover from the live server

      "tls": {"enabled": false, "root_certs_path": null},
      "metadata": {"authorization": "Bearer …"},
      "timeout_sec": 30,
      "actions": {                          # optional friendly name -> method
        "say_hello": {"method": "helloworld.Greeter/SayHello"},
        "chat":      {"method": "chat.Chat/Converse"}
      }
    }

Payload: for unary / server-streaming it's the request object as a dict; for
client-streaming / bidi it's ``{"messages": [ {...}, {...} ]}``. An action that
isn't in ``actions`` is treated as the method name itself.

A step whose action is ``"$discover"`` (or ``"__list_methods__"``) returns the
catalog of callable methods — handy after reflection, when the schema isn't
known up front.
"""

from __future__ import annotations

import logging
import time

from ..base.base_adapter import BaseAdapter, StepResult
from .descriptors import ProtoRegistry, compile_proto_sources, load_descriptor_set

log = logging.getLogger("payprobe.grpc_adapter")

DISCOVER_ACTIONS = {"$discover", "__discover__", "__list_methods__"}


class GrpcAdapter(BaseAdapter):

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        cfg = self.config
        self.target = (
            cfg.get("target") or f"{cfg.get('host', 'localhost')}:{cfg.get('port', 50051)}"
        )
        self.metadata = list((cfg.get("metadata") or {}).items())
        self.default_timeout = float(cfg.get("timeout_sec", 30))
        self.actions = cfg.get("actions", {})

        # Open the channel first — reflection discovery needs a live channel.
        import grpc  # lazy: grpcio is an optional dependency

        self._grpc = grpc
        tls = cfg.get("tls", {})
        if tls.get("enabled"):
            root = None
            if tls.get("root_certs_path"):
                with open(tls["root_certs_path"], "rb") as fh:
                    root = fh.read()
            creds = grpc.ssl_channel_credentials(root_certificates=root)
            self.channel = grpc.aio.secure_channel(self.target, creds)
        else:
            self.channel = grpc.aio.insecure_channel(self.target)

        self.registry = await self._build_registry()

    async def _build_registry(self) -> ProtoRegistry:
        """Assemble the schema from every configured source (set + proto + reflection)."""
        cfg = self.config
        registry: ProtoRegistry | None = None
        self.schema_sources: list[str] = []

        # 1. precompiled FileDescriptorSet
        if cfg.get("descriptor_set_b64") or cfg.get("descriptor_set_path"):
            registry = ProtoRegistry(
                load_descriptor_set(
                    b64=cfg.get("descriptor_set_b64"),
                    path=cfg.get("descriptor_set_path"),
                )
            )
            self.schema_sources.append("descriptor_set")

        # 2. custom-declared .proto sources, compiled on the fly
        if cfg.get("proto_inline") or cfg.get("proto_paths"):
            ds = compile_proto_sources(
                inline=cfg.get("proto_inline"),
                paths=cfg.get("proto_paths"),
                include_dirs=cfg.get("proto_include_dirs"),
            )
            registry = (registry or ProtoRegistry()).add_descriptor_set(ds)
            self.schema_sources.append("proto_sources")

        # 3. discovery via server reflection
        if cfg.get("reflection"):
            from .reflection import reflect_descriptor_set

            ds = await reflect_descriptor_set(
                self.channel,
                metadata=self.metadata or None,
                timeout=self.default_timeout,
            )
            registry = (registry or ProtoRegistry()).add_descriptor_set(ds)
            self.schema_sources.append("reflection")

        if registry is None:
            raise ValueError(
                "no gRPC schema configured — set one of descriptor_set_path / "
                "descriptor_set_b64, proto_inline / proto_paths, or reflection: true"
            )
        return registry

    async def disconnect(self) -> None:
        channel = getattr(self, "channel", None)
        if channel is not None:
            try:
                await channel.close()
            except Exception as exc:  # noqa: BLE001 — best-effort channel teardown
                log.debug("grpc adapter: error closing channel: %s", exc)

    async def health_check(self) -> bool:
        try:
            import asyncio

            await asyncio.wait_for(self.channel.channel_ready(), timeout=self.default_timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- step execution ------------------------------------------------------

    async def execute(self, action: str, payload: dict) -> StepResult:
        start = time.monotonic()

        if action in DISCOVER_ACTIONS:
            methods = self.registry.list_methods()
            return StepResult(
                success=True,
                request_payload={"action": action, "schema_sources": self.schema_sources},
                response_payload={"methods": methods, "count": len(methods)},
                duration_ms=int((time.monotonic() - start) * 1000),
                raw_log=f"[grpc] discover ({'+'.join(self.schema_sources)}) "
                f"→ {len(methods)} method(s)",
            )

        try:
            method_name = (self.actions.get(action) or {}).get("method") or action
            spec = self.registry.resolve_method(method_name)
            response = await self._invoke(spec, payload)
            return StepResult(
                success=True,
                request_payload={
                    "method": spec.full_method,
                    "rpc": spec.rpc_type,
                    "payload": payload,
                },
                response_payload=response,
                duration_ms=int((time.monotonic() - start) * 1000),
                raw_log=(
                    f"[grpc] → {spec.rpc_type} {spec.full_method}\n"
                    f"  REQ {payload}\n"
                    f"[grpc] ← reply\n"
                    f"  RES {response}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - report, don't raise
            return StepResult(
                success=False,
                request_payload=payload,
                response_payload={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _invoke(self, spec, payload: dict) -> dict:
        reg = self.registry
        ser = reg.serializer()
        deser = reg.deserializer(spec.output_desc)
        method = spec.full_method
        timeout = self.default_timeout
        md = self.metadata or None

        if spec.rpc_type == "unary":
            req = reg.build_message(spec.input_desc, payload)
            call = self.channel.unary_unary(
                method, request_serializer=ser, response_deserializer=deser
            )
            resp = await call(req, timeout=timeout, metadata=md)
            return reg.to_dict(resp)

        if spec.rpc_type == "server_stream":
            req = reg.build_message(spec.input_desc, payload)
            call = self.channel.unary_stream(
                method, request_serializer=ser, response_deserializer=deser
            )
            out = [reg.to_dict(r) async for r in call(req, timeout=timeout, metadata=md)]
            return {"messages": out, "count": len(out)}

        # client_stream / bidi: payload carries a list of request messages
        messages = payload.get("messages") or []

        async def _gen():
            for d in messages:
                yield reg.build_message(spec.input_desc, d)

        if spec.rpc_type == "client_stream":
            call = self.channel.stream_unary(
                method, request_serializer=ser, response_deserializer=deser
            )
            resp = await call(_gen(), timeout=timeout, metadata=md)
            return reg.to_dict(resp)

        # bidi
        call = self.channel.stream_stream(
            method, request_serializer=ser, response_deserializer=deser
        )
        out = [reg.to_dict(r) async for r in call(_gen(), timeout=timeout, metadata=md)]
        return {"messages": out, "count": len(out)}
