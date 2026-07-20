"""gRPC server reflection client — *discover* a service's schema at runtime.

Many gRPC servers expose the standard ``ServerReflection`` service, which serves
the very ``FileDescriptorProto``\\s the server was built from. This module drives
that API over a live channel and assembles a self-contained ``FileDescriptorSet``
— the same bytes you'd get from ``protoc --include_imports`` — so the adapter can
call any method without anyone shipping a descriptor set or ``.proto`` file.

The reflection request/response message types come from ``grpc_reflection`` (the
``grpcio-reflection`` optional dependency). Both the modern ``v1`` and the older
``v1alpha`` reflection services are tried, since servers vary in which they host.
``grpc`` itself is imported lazily by the caller; everything here is async.
"""

from __future__ import annotations

from typing import Any

# Reflection service method paths (bidi stream of request/response).
_V1_METHOD = "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo"
_V1ALPHA_METHOD = "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo"


def _load_reflection_pb():
    """Return (request_cls, method_path) for whichever reflection API is present."""
    try:  # modern API first
        from grpc_reflection.v1 import reflection_pb2 as pb  # type: ignore

        return pb, _V1_METHOD, _V1ALPHA_FALLBACK
    except ImportError:
        pass
    try:
        from grpc_reflection.v1alpha import reflection_pb2 as pb  # type: ignore

        return pb, _V1ALPHA_METHOD, None
    except ImportError as exc:  # pragma: no cover - only without the dep
        raise RuntimeError(
            "gRPC reflection discovery needs grpcio-reflection — "
            "install with: pip install grpcio-reflection"
        ) from exc


# Sentinel: when the v1 pb2 is loaded we still want to retry on v1alpha path if
# the server only hosts the older service.
_V1ALPHA_FALLBACK = object()


async def reflect_descriptor_set(channel, *, metadata=None, timeout: float = 30.0) -> bytes:
    """Discover the server's schema and return it as a ``FileDescriptorSet``.

    Lists the server's services, fetches the file declaring each one, then walks
    transitive ``import`` dependencies until every referenced file is collected.
    """
    from google.protobuf import descriptor_pb2

    pb, method, fallback = _load_reflection_pb()
    md = list(metadata) if metadata else None

    async def _request_response(make_request):
        """Open a one-shot reflection stream: send one request, read one reply.

        Retries on the v1alpha path if the v1 service isn't routed by the server.
        """
        Req = pb.ServerReflectionRequest
        Resp = pb.ServerReflectionResponse

        async def _run(path):
            async def _gen():
                yield make_request(Req)

            call = channel.stream_stream(
                path,
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=Resp.FromString,
            )
            async for resp in call(_gen(), timeout=timeout, metadata=md):
                return resp
            return None

        try:
            return await _run(method)
        except Exception:  # noqa: BLE001 - try the legacy service before giving up
            if fallback is _V1ALPHA_FALLBACK:
                return await _run(_V1ALPHA_METHOD)
            raise

    # 1. list services
    resp = await _request_response(lambda Req: Req(list_services=""))
    service_names = [s.name for s in resp.list_services_response.service]

    files: dict[str, Any] = {}  # name -> FileDescriptorProto

    def _ingest(resp) -> None:
        for raw in resp.file_descriptor_response.file_descriptor_proto:
            fdp = descriptor_pb2.FileDescriptorProto()
            fdp.ParseFromString(raw)
            files.setdefault(fdp.name, fdp)

    # 2. fetch the file declaring each service symbol
    for sym in service_names:
        # skip the reflection service's own definition; not needed to call others
        if sym.startswith("grpc.reflection."):
            continue
        resp = await _request_response(lambda Req, s=sym: Req(file_containing_symbol=s))
        _ingest(resp)

    # 3. resolve transitive import dependencies
    seen_requests: set[str] = set()
    changed = True
    while changed:
        changed = False
        for fdp in list(files.values()):
            for dep in fdp.dependency:
                if dep in files or dep in seen_requests:
                    continue
                seen_requests.add(dep)
                resp = await _request_response(lambda Req, d=dep: Req(file_by_filename=d))
                _ingest(resp)
                changed = True

    fds = descriptor_pb2.FileDescriptorSet()
    fds.file.extend(files.values())
    return fds.SerializeToString()
