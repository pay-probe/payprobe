"""Dynamic protobuf descriptor registry for the general gRPC adapter.

Lets the adapter call *any* gRPC method without generated stubs. The schema can
come from three places, all of which boil down to a set of ``FileDescriptorProto``
loaded into a single ``DescriptorPool``:

* a precompiled ``FileDescriptorSet``
  (``protoc --include_imports --descriptor_set_out=...``) — :func:`load_descriptor_set`;
* **custom-declared** ``.proto`` source (inline text or files on disk), compiled
  on the fly — :func:`compile_proto_sources`;
* **discovered** from a live server over gRPC server reflection — see
  ``reflection.py`` (it produces a ``FileDescriptorSet`` fed in here).

Once loaded, a method is resolved by name to learn its request / response message
types and whether each side streams. Requests are built from plain dicts
(``json_format``) and responses are returned as dicts.

protobuf-only (no grpcio import here) so it is unit-testable on its own.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory


@dataclass
class MethodSpec:
    full_method: str  # "/package.Service/Method"
    input_desc: Any  # Descriptor of the request message
    output_desc: Any  # Descriptor of the response message
    client_streaming: bool
    server_streaming: bool

    @property
    def rpc_type(self) -> str:
        if self.client_streaming and self.server_streaming:
            return "bidi"
        if self.server_streaming:
            return "server_stream"
        if self.client_streaming:
            return "client_stream"
        return "unary"


def load_descriptor_set(
    *, b64: str | None = None, path: str | None = None, raw: bytes | None = None
) -> bytes:
    if raw is not None:
        return raw
    if b64:
        return base64.b64decode(b64)
    if path:
        return Path(path).read_bytes()
    raise ValueError(
        "no descriptor set provided " "(set descriptor_set_b64 or descriptor_set_path)"
    )


def compile_proto_sources(
    *,
    inline: Any = None,
    paths: Iterable[str] | None = None,
    include_dirs: Iterable[str] | None = None,
) -> bytes:
    """Compile custom-declared ``.proto`` source into a ``FileDescriptorSet``.

    Lets a connection declare its schema directly instead of shipping a
    precompiled descriptor set. ``inline`` is raw ``.proto`` text — either a
    single string, or a ``{filename: text}`` mapping, or a list of strings
    (auto-named ``inline_0.proto`` …). ``paths`` points at ``.proto`` files on
    the worker host. ``include_dirs`` are extra ``-I`` import roots.

    Uses ``grpc_tools.protoc`` (the ``grpcio-tools`` optional dependency) with
    ``--include_imports`` so the result is self-contained, exactly like the
    descriptor set you'd produce by hand.
    """
    try:
        from grpc_tools import protoc
        import grpc_tools
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise RuntimeError(
            "compiling .proto sources needs grpcio-tools — "
            "install with: pip install grpcio-tools"
        ) from exc

    import tempfile

    proto_files: list[str] = []
    includes: list[str] = list(include_dirs or [])

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        # inline text -> temp files
        sources: dict[str, str] = {}
        if isinstance(inline, str):
            sources["inline_0.proto"] = inline
        elif isinstance(inline, dict):
            sources.update(inline)
        elif isinstance(inline, (list, tuple)):
            for i, text in enumerate(inline):
                sources[f"inline_{i}.proto"] = text
        for name, text in sources.items():
            fp = td_path / name
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(text)
            proto_files.append(str(fp))
        if sources:
            includes.append(str(td_path))

        for p in paths or []:
            proto_files.append(str(p))
            includes.append(str(Path(p).resolve().parent))

        if not proto_files:
            raise ValueError("no .proto sources provided (set proto_inline or proto_paths)")

        out = td_path / "_bundle.pb"
        # grpc_tools bundles the well-known types (google/protobuf/*.proto).
        wkt = Path(grpc_tools.__file__).parent / "_proto"

        args = ["protoc", f"--descriptor_set_out={out}", "--include_imports"]
        for d in dict.fromkeys(includes):  # de-dupe, keep order
            args.append(f"-I{d}")
        args.append(f"-I{wkt}")
        args.extend(proto_files)

        rc = protoc.main(args)
        if rc != 0:
            raise RuntimeError(f"protoc failed (exit {rc}) compiling {proto_files}")
        return out.read_bytes()


class ProtoRegistry:
    """Holds a ``DescriptorPool`` built from one or more ``FileDescriptorSet``\\s.

    Constructed empty or from an initial set; further sets (e.g. discovered via
    reflection) can be merged in with :meth:`add_descriptor_set`. Files are added
    in dependency order and duplicates are skipped, so overlapping sets (shared
    imports, well-known types) merge cleanly.
    """

    def __init__(self, descriptor_set: bytes | None = None) -> None:
        self.pool = descriptor_pool.DescriptorPool()
        self._files: dict[str, Any] = {}  # name -> FileDescriptorProto
        self._added: set[str] = set()
        if descriptor_set is not None:
            self.add_descriptor_set(descriptor_set)

    # -- loading -------------------------------------------------------------

    def add_descriptor_set(self, descriptor_set: bytes) -> "ProtoRegistry":
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(descriptor_set)
        self.add_file_protos(fds.file)
        return self

    def add_file_protos(self, file_protos: Iterable[Any]) -> "ProtoRegistry":
        for f in file_protos:
            self._files.setdefault(f.name, f)
        for name in list(self._files):
            self._add(name)
        return self

    def _add(self, name: str) -> None:
        if name in self._added or name not in self._files:
            return
        f = self._files[name]
        for dep in f.dependency:  # dependencies first
            self._add(dep)
        try:
            self.pool.Add(f)
        except TypeError:
            # Already present in the pool (e.g. a well-known type shared between
            # a static set and a reflected one) — treat as a no-op.
            pass
        self._added.add(name)

    # -- message classes -----------------------------------------------------

    def message_class(self, descriptor):
        if hasattr(message_factory, "GetMessageClass"):  # protobuf >= 4.22
            return message_factory.GetMessageClass(descriptor)
        return message_factory.MessageFactory(self.pool).GetPrototype(descriptor)

    # -- method resolution ---------------------------------------------------

    def resolve_method(self, method: str) -> MethodSpec:
        """``"pkg.Service/Method"`` or ``"/pkg.Service/Method"`` -> MethodSpec."""
        svc_name, _, meth_name = method.strip("/").partition("/")
        if not meth_name:
            raise ValueError(f"method must be 'package.Service/Method', got {method!r}")
        svc = self.pool.FindServiceByName(svc_name)
        meth = svc.FindMethodByName(meth_name)
        return MethodSpec(
            full_method=f"/{svc_name}/{meth_name}",
            input_desc=meth.input_type,
            output_desc=meth.output_type,
            client_streaming=meth.client_streaming,
            server_streaming=meth.server_streaming,
        )

    # -- discovery -----------------------------------------------------------

    def list_services(self) -> list[str]:
        """Fully-qualified names of every service in the pool."""
        names: list[str] = []
        for fname in self._added:
            fdesc = self.pool.FindFileByName(fname)
            names.extend(svc.full_name for svc in fdesc.services_by_name.values())
        return sorted(set(names))

    def list_methods(self) -> list[dict]:
        """Catalog every method: service, method, rpc_type, input/output types.

        Powers schema *discovery* — a step (or the portal) can ask the adapter
        what it can call without knowing the proto up front.
        """
        out: list[dict] = []
        for svc_name in self.list_services():
            svc = self.pool.FindServiceByName(svc_name)
            for meth in svc.methods:
                spec = MethodSpec(
                    full_method=f"/{svc_name}/{meth.name}",
                    input_desc=meth.input_type,
                    output_desc=meth.output_type,
                    client_streaming=meth.client_streaming,
                    server_streaming=meth.server_streaming,
                )
                out.append(
                    {
                        "service": svc_name,
                        "method": meth.name,
                        "full_method": f"{svc_name}/{meth.name}",
                        "rpc_type": spec.rpc_type,
                        "input_type": meth.input_type.full_name,
                        "output_type": meth.output_type.full_name,
                    }
                )
        return out

    # -- (de)serialization ---------------------------------------------------

    def build_message(self, descriptor, data: dict | None):
        msg = self.message_class(descriptor)()
        if data:
            json_format.ParseDict(data, msg, ignore_unknown_fields=True)
        return msg

    def to_dict(self, msg) -> dict:
        return json_format.MessageToDict(msg, preserving_proto_field_name=True)

    def serializer(self):
        return lambda m: m.SerializeToString()

    def deserializer(self, descriptor):
        cls = self.message_class(descriptor)
        return cls.FromString
