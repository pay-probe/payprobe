# gRPC adapter

The gRPC adapter calls any method on a gRPC service **without generated stubs**.
It invokes methods dynamically from protobuf descriptors, supporting all four RPC
shapes — unary, server-streaming, client-streaming, and bidirectional streaming.
A scenario step just names a method and passes a JSON-shaped payload.

`grpcio` is an optional dependency, imported lazily at connect. The worker
package imports fine without it; the adapter only registers when `grpcio` is
installed.

## Where the schema comes from

The adapter needs to know which methods exist and their message types. That
schema can come from any of three sources — and they can be combined (each is
merged into the same descriptor pool):

1. **A precompiled `FileDescriptorSet`** — `descriptor_set_path` /
   `descriptor_set_b64`. Best for reproducible runs.
2. **Custom-declared `.proto` source**, compiled on the fly — `proto_inline`
   (raw text) and/or `proto_paths` (files on the worker host). Needs
   `grpcio-tools`.
3. **Discovery via server reflection** — `reflection: true`. The adapter asks the
   running server for its own descriptors, so you ship neither a `.pb` nor a
   `.proto`. Needs `grpcio-reflection`, and the server must expose the
   `ServerReflection` service.

Use whichever fits, or mix them (e.g. discover most of a server but pin one
message from a local `.proto`).

## 1. Install grpcio

```bash
pip install grpcio grpcio-tools grpcio-reflection
```

`grpcio-tools` is only needed for custom `.proto` compilation, `grpcio-reflection`
only for discovery.

## 2. Option A — compile a descriptor set

The classic path. Compile a `FileDescriptorSet`, not generated code:

```bash
python -m grpc_tools.protoc -I. \
  --include_imports \
  --descriptor_set_out=bundle.pb \
  your_service.proto
```

`--include_imports` bundles every imported proto so message types resolve at
runtime. Point the adapter at `bundle.pb` via `descriptor_set_path` (or paste it
as base64 in `descriptor_set_b64`).

## 2. Option B — declare the proto inline

Skip the build step and hand the adapter `.proto` source directly. The adapter
compiles it with `grpc_tools.protoc` at connect:

```json
{
  "adapter": "grpc",
  "target": "localhost:50051",
  "proto_inline": "syntax = \"proto3\"; package demo; service Echo { rpc Say (Msg) returns (Msg); } message Msg { string text = 1; }",
  "actions": { "say": { "method": "demo.Echo/Say" } }
}
```

`proto_inline` accepts a single string, a list of strings, or a
`{ "filename.proto": "<text>" }` map (use a map when files `import` each other).
`proto_paths` points at `.proto` files already on the worker host, and
`proto_include_dirs` adds extra `-I` import roots.

## 2. Option C — discover via reflection

If the server exposes `ServerReflection`, no schema file is needed at all:

```json
{
  "adapter": "grpc",
  "target": "localhost:50051",
  "reflection": true
}
```

At connect the adapter lists the server's services, pulls the file descriptors
for each, and resolves their imports. To see what was discovered, run a step with
action `"$discover"` — it returns every callable method with its RPC type and
message types:

```json
{ "id": "list", "target": "discovered_grpc", "action": "$discover" }
```

## 3. Add a connection (portal)

In **Connections**, create a connection and set **Adapter → gRPC**. Choose a
schema source:

- **Descriptor set path** — path to `bundle.pb` on the worker host (or paste the
  set as base64); or
- **Proto source** — paste `.proto` text / point at files; or
- **Reflection** — toggle on to discover the schema from the server.

Then fill in:

- **Target** — `host:port` (e.g. `localhost:50051`).
- **TLS** — enable for a secure channel; optionally point at a root certs file.
- **Call metadata** — headers sent on every call (e.g. `authorization`).
- **Actions** — friendly names mapped to `package.Service/Method`.

Scenarios then target the connection by name. **Test** opens the channel and
waits for it to become ready.

## 4. Or declare it in an environment file

See `examples/environments/grpc.json`:

```json
{
  "adapters": {
    "payments_grpc": {
      "adapter": "grpc",
      "target": "localhost:50051",
      "descriptor_set_path": "bundle.pb",
      "timeout_sec": 30,
      "tls": { "enabled": false },
      "metadata": { "authorization": "Bearer <token>" },
      "actions": {
        "authorize": { "method": "payments.PaymentService/Authorize" }
      }
    },
    "discovered_grpc": {
      "adapter": "grpc",
      "target": "localhost:50051",
      "reflection": true
    }
  }
}
```

Config keys: `target` (or `host` + `port`); **schema** — `descriptor_set_path` /
`descriptor_set_b64`, `proto_inline` / `proto_paths` / `proto_include_dirs`,
`reflection`; `tls.enabled` + `tls.root_certs_path`; `metadata` (per-call gRPC
metadata); `timeout_sec`; `actions` (friendly name → method).

## 5. Call it from a step

`action` is a friendly name from `actions`, or a raw `package.Service/Method`.
`payload` is the request message as a dict:

```json
{
  "id": "auth", "target": "payments_grpc", "action": "authorize",
  "payload": { "pan": "411111******1111", "amount": 1000, "currency": "840" },
  "assertions": [
    { "field": "approved", "operator": "eq", "expected": true },
    { "field": "auth_code", "operator": "present" }
  ]
}
```

For **client-streaming** or **bidirectional** RPCs, send several messages with a
`messages` array instead of a flat object:

```json
"payload": { "messages": [ { "text": "hi" }, { "text": "bye" } ] }
```

Server-streaming responses are collected so assertions can run against them.
