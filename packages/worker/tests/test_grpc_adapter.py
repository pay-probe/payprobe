"""General gRPC adapter — descriptor-driven dynamic invocation.

The protobuf-only core (descriptor pool, method resolution, message build/parse)
and the adapter's RPC-type dispatch are tested here without grpcio: a fake async
channel stands in for a live server, so all four RPC shapes are exercised. A
real end-to-end test against a gRPC server belongs in an integration suite where
grpcio is installed.
"""

import pytest
from google.protobuf import descriptor_pb2 as dpb

from worker.adapters.grpc.adapter import GrpcAdapter
from worker.adapters.grpc.descriptors import (
    ProtoRegistry,
    compile_proto_sources,
)


def _descriptor_set() -> bytes:
    """A small Greeter service with unary + server/client/bidi streaming."""
    f = dpb.FileDescriptorProto(name="greet.proto", package="greet", syntax="proto3")

    req = f.message_type.add()
    req.name = "HelloRequest"
    fld = req.field.add()
    fld.name, fld.number = "name", 1
    fld.type = dpb.FieldDescriptorProto.TYPE_STRING
    fld.label = dpb.FieldDescriptorProto.LABEL_OPTIONAL

    rep = f.message_type.add()
    rep.name = "HelloReply"
    fld2 = rep.field.add()
    fld2.name, fld2.number = "message", 1
    fld2.type = dpb.FieldDescriptorProto.TYPE_STRING
    fld2.label = dpb.FieldDescriptorProto.LABEL_OPTIONAL

    svc = f.service.add()
    svc.name = "Greeter"
    for name, cs, ss in [
        ("SayHello", False, False),
        ("SayHellos", False, True),
        ("LotsOfGreetings", True, False),
        ("BidiHello", True, True),
    ]:
        m = svc.method.add()
        m.name = name
        m.input_type = ".greet.HelloRequest"
        m.output_type = ".greet.HelloReply"
        m.client_streaming, m.server_streaming = cs, ss

    fds = dpb.FileDescriptorSet()
    fds.file.append(f)
    return fds.SerializeToString()


DS = _descriptor_set()


# -- descriptor registry ------------------------------------------------------


def test_resolve_method_rpc_types():
    reg = ProtoRegistry(DS)
    assert reg.resolve_method("greet.Greeter/SayHello").rpc_type == "unary"
    assert reg.resolve_method("/greet.Greeter/SayHellos").rpc_type == "server_stream"
    assert reg.resolve_method("greet.Greeter/LotsOfGreetings").rpc_type == "client_stream"
    assert reg.resolve_method("greet.Greeter/BidiHello").rpc_type == "bidi"


def test_build_and_roundtrip_message():
    reg = ProtoRegistry(DS)
    spec = reg.resolve_method("greet.Greeter/SayHello")
    msg = reg.build_message(spec.input_desc, {"name": "Ada"})
    raw = msg.SerializeToString()
    back = reg.deserializer(spec.input_desc)(raw)
    assert reg.to_dict(back) == {"name": "Ada"}


# -- adapter dispatch (fake channel) -----------------------------------------


class _FakeMultiCallable:
    """Echoes the request's `name` into a HelloReply for every RPC shape."""

    def __init__(self, reg, out_desc, kind):
        self._reg, self._out, self._kind = reg, out_desc, kind

    def _reply(self, name):
        return self._reg.build_message(self._out, {"message": f"hi {name}"})

    def __call__(self, request, timeout=None, metadata=None):
        if self._kind in ("unary", "server_stream"):
            name = self._reg.to_dict(request).get("name", "")
            if self._kind == "unary":

                async def _coro():
                    return self._reply(name)

                return _coro()

            async def _agen():
                yield self._reply(name + "-1")
                yield self._reply(name + "-2")

            return _agen()

        # client_stream / bidi: `request` is an async iterator of messages
        if self._kind == "client_stream":

            async def _coro2():
                names = [self._reg.to_dict(m).get("name", "") async for m in request]
                return self._reply(",".join(names))

            return _coro2()

        async def _agen2():
            async for m in request:
                yield self._reply(self._reg.to_dict(m).get("name", ""))

        return _agen2()


class _FakeChannel:
    def __init__(self, reg):
        self._reg = reg

    def _mc(self, kind, response_deserializer):
        # the adapter resolves the output descriptor; reuse the reply message
        out = self._reg.pool.FindMessageTypeByName("greet.HelloReply")
        return _FakeMultiCallable(self._reg, out, kind)

    def unary_unary(self, m, request_serializer=None, response_deserializer=None):
        return self._mc("unary", response_deserializer)

    def unary_stream(self, m, request_serializer=None, response_deserializer=None):
        return self._mc("server_stream", response_deserializer)

    def stream_unary(self, m, request_serializer=None, response_deserializer=None):
        return self._mc("client_stream", response_deserializer)

    def stream_stream(self, m, request_serializer=None, response_deserializer=None):
        return self._mc("bidi", response_deserializer)


def _adapter():
    a = GrpcAdapter(
        {
            "target": "x:1",
            "descriptor_set_b64": None,
            "actions": {"hello": {"method": "greet.Greeter/SayHello"}},
        }
    )
    a.registry = ProtoRegistry(DS)
    a.actions = a.config["actions"]
    a.metadata = []
    a.default_timeout = 5
    a.channel = _FakeChannel(a.registry)
    return a


async def test_unary_call_by_friendly_action():
    a = _adapter()
    r = await a.execute("hello", {"name": "Ada"})
    assert r.success
    assert r.response_payload == {"message": "hi Ada"}
    assert r.request_payload["rpc"] == "unary"


async def test_server_streaming_returns_list():
    a = _adapter()
    r = await a.execute("greet.Greeter/SayHellos", {"name": "Ada"})
    assert r.success and r.response_payload["count"] == 2
    assert r.response_payload["messages"][0] == {"message": "hi Ada-1"}


async def test_client_streaming_aggregates():
    a = _adapter()
    r = await a.execute(
        "greet.Greeter/LotsOfGreetings", {"messages": [{"name": "a"}, {"name": "b"}]}
    )
    assert r.success
    assert r.response_payload == {"message": "hi a,b"}


async def test_bidi_streaming_echoes_each():
    a = _adapter()
    r = await a.execute("greet.Greeter/BidiHello", {"messages": [{"name": "a"}, {"name": "b"}]})
    assert r.success and r.response_payload["count"] == 2
    assert [m["message"] for m in r.response_payload["messages"]] == ["hi a", "hi b"]


async def test_unknown_method_is_reported_not_raised():
    a = _adapter()
    r = await a.execute("greet.Greeter/Nope", {"name": "x"})
    assert r.success is False and r.error


# -- schema sources: merge + discovery ---------------------------------------


def test_registry_empty_then_merge():
    """ProtoRegistry can start empty and have sets merged in (reflection path)."""
    reg = ProtoRegistry()
    assert reg.list_methods() == []
    reg.add_descriptor_set(DS)
    assert reg.resolve_method("greet.Greeter/SayHello").rpc_type == "unary"
    # merging the same set again is idempotent (overlapping files are skipped)
    reg.add_descriptor_set(DS)
    assert len(reg.list_methods()) == 4


def test_list_methods_catalog():
    reg = ProtoRegistry(DS)
    methods = {m["full_method"]: m for m in reg.list_methods()}
    assert methods["greet.Greeter/SayHello"]["rpc_type"] == "unary"
    assert methods["greet.Greeter/BidiHello"]["rpc_type"] == "bidi"
    assert methods["greet.Greeter/SayHello"]["input_type"] == "greet.HelloRequest"
    assert methods["greet.Greeter/SayHello"]["output_type"] == "greet.HelloReply"


async def test_discover_action_lists_methods():
    a = _adapter()
    a.schema_sources = ["descriptor_set"]
    r = await a.execute("$discover", {})
    assert r.success
    assert r.response_payload["count"] == 4
    fulls = {m["full_method"] for m in r.response_payload["methods"]}
    assert "greet.Greeter/SayHello" in fulls
    assert r.request_payload["schema_sources"] == ["descriptor_set"]


# -- custom-declared .proto compilation --------------------------------------

GREETER_PROTO = """
syntax = "proto3";
package greet;
message HelloRequest { string name = 1; }
message HelloReply { string message = 1; }
service Greeter {
  rpc SayHello (HelloRequest) returns (HelloReply);
  rpc SayHellos (HelloRequest) returns (stream HelloReply);
}
"""


def _have_grpc_tools() -> bool:
    try:
        import grpc_tools  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _have_grpc_tools(), reason="grpcio-tools not installed")
def test_compile_inline_proto_to_registry():
    ds = compile_proto_sources(inline=GREETER_PROTO)
    reg = ProtoRegistry(ds)
    assert reg.resolve_method("greet.Greeter/SayHello").rpc_type == "unary"
    assert reg.resolve_method("greet.Greeter/SayHellos").rpc_type == "server_stream"
    msg = reg.build_message(
        reg.resolve_method("greet.Greeter/SayHello").input_desc, {"name": "Ada"}
    )
    assert reg.to_dict(msg) == {"name": "Ada"}


@pytest.mark.skipif(not _have_grpc_tools(), reason="grpcio-tools not installed")
def test_compile_proto_named_map():
    ds = compile_proto_sources(inline={"greeter.proto": GREETER_PROTO})
    reg = ProtoRegistry(ds)
    assert len(reg.list_methods()) == 2


def test_compile_proto_requires_sources():
    with pytest.raises((ValueError, RuntimeError)):
        compile_proto_sources()
