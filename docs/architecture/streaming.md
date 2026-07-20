# Live Run Streaming

How run events get from the worker engine to the portal in real time, durably,
and with reconnect-resume.

```
WorkerEngine ──StreamSink──▶ StreamBackbone ──▶ Orchestrator WS ──▶ Angular
  emits RunEvents            durable, ordered,    GET /runs/{id}/      RunMonitorService
                             replayable           stream (resume)      (auto-reconnect)
```

## Why not plain Redis pub/sub

The original spec used Redis **pub/sub** for `run:{id}:stream`. Pub/sub is
fire-and-forget: any client not connected at the instant an event fires never
sees it. A dropped WebSocket or a late-joining watcher loses history. For ~30
people watching multi-minute runs that's unacceptable.

We use an append-only **stream** instead: every event is stored with a
monotonic id, so a consumer can say "give me everything after id X" and replay
what it missed before tailing live. That single primitive is what makes
WebSocket reconnect-resume work.

## The three layers

1. **Engine async-generator API** — `async for event in engine.iter_run(scenarios, run_id)`
   yields each `RunEvent` live while the base sink still receives everything.
   (`packages/worker/engine/engine.py`)

2. **Durable backbone** — `StreamBackbone` with two implementations
   (`packages/worker/engine/stream.py`):
   - `InMemoryStreamBackbone` — single-process, no infra; dev/CI and the mock path.
   - `RedisStreamBackbone` — Redis Streams (`XADD`/`XRANGE`/`XREAD`); production.
   Both expose `append(run_id, event) -> id` and
   `stream(run_id, after_id) -> AsyncIterator[(id, event)]` (replay then tail).

3. **WebSocket transport** — `GET /runs/{id}/stream` in the orchestrator
   (`packages/orchestrator/api/main.py`). On connect it reads
   `?last_event_id=<id>`, replays everything after it, then tails live. The
   channel is observe-only; `cancel` is the REST `POST /runs/{id}/cancel`, so the
   stream stays a clean one-way feed.

On the client, `RunMonitorService.stream(runId)`
(`packages/portal/src/app/run-monitor/`) tracks the last event id, reconnects
with exponential backoff on unexpected drops, resumes via `last_event_id`, and
completes on `run.completed`. `phases()` and `steps()` expose filtered views.

## Resume contract

Every WebSocket frame is `{ "id": "<cursor>", "type": ..., "run_id": ..., "data": {...}, "ts": ... }`.
The client persists the latest `id`; on reconnect it sends it back as
`last_event_id`. With `RedisStreamBackbone` the cursor is the native Redis entry
id, so resume maps straight onto `XRANGE (id` / `XREAD`.

## Switching to Redis

Set `REDIS_URL` for the orchestrator; it auto-selects `RedisStreamBackbone`.
Unset, it uses the in-memory backbone. No code change.

## Running it live (mock)

```bash
# from packages/
pip install -e worker -e orchestrator         # or install deps directly
uvicorn orchestrator.api.main:app --port 8100  # REDIS_URL optional
curl -XPOST localhost:8100/runs -H 'content-type: application/json' -d '{}'
# then connect a WebSocket to ws://localhost:8100/runs/<run_id>/stream
```

## Tests

`packages/orchestrator/tests/test_stream.py` covers backbone replay, resume
from a cursor, live tailing, the WS endpoint (replay + resume), and the
`POST /runs` lifecycle. `packages/worker/tests/test_engine.py` covers
`iter_run`. Run: `cd packages && python -m pytest worker/tests orchestrator/tests`.

> Note: the WS endpoint is tested by invoking the endpoint coroutine with a fake
> WebSocket rather than starlette's `TestClient`, whose portal threads deadlock
> under `pytest-asyncio`'s managed loop. The transport works under real uvicorn
> (verified manually); only the in-process test client has the conflict.
