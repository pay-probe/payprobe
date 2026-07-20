# gRPC Adapter (`GrpcAdapter`)

A **general, config-driven gRPC adapter**. It calls any method on a gRPC
service with **no generated stubs** by invoking methods dynamically from
protobuf descriptors. All four RPC shapes are supported:

| RPC type | Derived from descriptors | Payload | Response |
|---|---|---|---|
| **unary** | `client=no, server=no` | request object (dict) | response dict |
| **server-streaming** | `server=yes` | request object (dict) | `{messages: [...], count}` |
| **client-streaming** | `client=yes` | `{messages: [ {...}, … ]}` | response dict |
| **bidirectional** | `client=yes, server=yes` | `{messages: [ {...}, … ]}` | `{messages: [...], count}` |

The request/response message types and streaming flags come from the
descriptors, so a step only names a method and passes a JSON-shaped payload.

`grpcio` is an **optional** dependency — `grpc` is imported lazily at
`connect()`, so the worker runs fine without it until a `grpc` target is used.
Install with `pip install -e "packages/worker[grpc]"`.

## Where the schema comes from

The method/message schema can be supplied three ways (combinable — all merge
into one descriptor pool):

1. **Precompiled descriptor set** — `descriptor_set_path` / `descriptor_set_b64`.
2. **Custom-declared `.proto`** compiled on the fly — `proto_inline` (raw text:
   string, list, or `{name: text}` map) / `proto_paths` (+ `proto_include_dirs`).
   Needs `grpcio-tools`.
3. **Discovery via server reflection** — `reflection: true`. Pulls the server's
   own descriptors at connect; needs `grpcio-reflection`.

Produce a descriptor set with:

```bash
python -m grpc_tools.protoc \
  --include_imports \
  --descriptor_set_out=bundle.pb \
  -I proto proto/helloworld.proto
```

## Config

```jsonc
{
  "target": "localhost:50051",            // or "host" + "port"

  // --- schema: pick one or more ---
  "descriptor_set_path": "bundle.pb",     // or "descriptor_set_b64": "<base64>"
  "proto_inline": "syntax=\"proto3\"; ...",  // raw .proto text
  "proto_paths": ["proto/helloworld.proto"], // .proto files on the worker host
  "proto_include_dirs": ["proto"],        // extra -I import roots
  "reflection": true,                     // discover from the live server

  "tls": { "enabled": false, "root_certs_path": null },
  "metadata": { "authorization": "Bearer …" },  // sent on every call
  "timeout_sec": 30,
  "actions": {                            // optional friendly name -> method
    "say_hello":  { "method": "helloworld.Greeter/SayHello" },
    "say_hellos": { "method": "helloworld.Greeter/SayHellos" },
    "lots":       { "method": "helloworld.Greeter/LotsOfGreetings" },
    "chat":       { "method": "helloworld.Greeter/BidiHello" }
  }
}
```

A step's `action` is looked up in `actions`; if it isn't there, the action is
treated as the method name directly (e.g. `helloworld.Greeter/SayHello`). The
reserved action `$discover` returns the catalog of callable methods — useful
after reflection.

## Examples

```jsonc
// unary
{ "target": "say_hello", "payload": { "name": "Ada" } }
// -> { "message": "Hello Ada" }

// bidirectional streaming
{ "target": "chat",
  "payload": { "messages": [ { "name": "a" }, { "name": "b" } ] } }
// -> { "messages": [ {…}, {…} ], "count": 2 }
```

Register it by naming a `grpc` adapter in your environment (or any instance name
with `"adapter": "grpc"`), the same way the universal TCP adapter is configured.
