"""NatsAdapter (ADR-0006): the outbound NATS client resolves publish / request /
js_publish against a broker, encodes payloads through the codec layer, and maps
results into a StepResult. The broker is a fake — no nats-py server needed.

An opt-in integration test (PAYPROBE_NATS_URL set) exercises a real broker; it is
skipped otherwise so the suite runs anywhere.
"""

import os

import pytest

# nats-py is an optional dep (ADR-0006): its absence skips this suite, exactly
# like aiohttp/grpcio — environmental, not a regression.
pytest.importorskip("nats")

import worker.adapters.nats.adapter as nats_adapter
from worker.adapters.nats.adapter import NatsAdapter
from worker.adapters.nats import codecs


class FakeMsg:
    def __init__(self, subject, data, reply=""):
        self.subject = subject
        self.data = data
        self.reply = reply


class FakeAck:
    def __init__(self, stream="PAY", seq=7, duplicate=False):
        self.stream = stream
        self.seq = seq
        self.duplicate = duplicate


class NotFound(Exception):
    """Stand-in for nats.js.errors.NotFoundError."""


class FakeJetStream:
    def __init__(self, client):
        self.client = client

    async def publish(self, subject, data, **kw):
        self.client.js_published.append((subject, data))
        return FakeAck()

    async def stream_info(self, name):
        if name not in self.client.streams:
            raise NotFound(name)
        return {"name": name}

    async def add_stream(self, name, subjects=None, **kw):
        self.client.streams[name] = list(subjects or [])
        self.client.stream_adds.append(name)
        self.client.stream_add_kwargs = kw

    async def delete_stream(self, name):
        self.client.streams.pop(name, None)
        self.client.stream_deletes.append(name)

    async def consumer_info(self, stream, durable):
        if (stream, durable) not in self.client.consumers:
            raise NotFound(durable)
        return {"durable": durable}

    async def add_consumer(self, stream, cfg):
        durable = getattr(cfg, "durable_name", None)
        self.client.consumers.add((stream, durable))
        self.client.consumer_adds.append((stream, durable))

    async def delete_consumer(self, stream, durable):
        self.client.consumers.discard((stream, durable))
        self.client.consumer_deletes.append((stream, durable))


class FakeNats:
    """A stand-in NATS client with the surface NatsAdapter uses."""

    def __init__(self):
        self.published: list[tuple[str, bytes]] = []
        self.js_published: list[tuple[str, bytes]] = []
        self.requests: list[tuple[str, bytes, float]] = []
        self.flushed = False
        self.drained = False
        self.is_connected = True
        #: canned reply bytes for request()
        self.reply_bytes = b'{"decision":"APPROVE"}'
        #: JetStream state — which streams/consumers exist and the lifecycle log
        self.streams: dict[str, list] = {}
        self.consumers: set = set()
        self.stream_adds: list = []
        self.stream_deletes: list = []
        self.consumer_adds: list = []
        self.consumer_deletes: list = []

    async def publish(self, subject, data, **kw):
        self.published.append((subject, data))

    async def request(self, subject, data, timeout=None):
        self.requests.append((subject, data, timeout))
        return FakeMsg("_INBOX.abc", self.reply_bytes, reply="")

    def jetstream(self):
        return FakeJetStream(self)

    async def flush(self, timeout=None):
        self.flushed = True

    async def drain(self):
        self.drained = True

    async def close(self):
        pass


def _patch(monkeypatch):
    fake = FakeNats()

    async def fake_connect(servers, **opts):
        fake.servers = servers
        fake.opts = opts
        return fake

    monkeypatch.setattr(nats_adapter, "_nats_connect", fake_connect)
    return fake


async def test_publish_encodes_json_and_uses_default_subject(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1", "port": 4222, "subject": "pay.auth"})
    await a.connect()
    res = await a.execute("publish", {"amount": 100})
    assert res.success
    subject, data = fake.published[-1]
    assert subject == "pay.auth"
    assert data == b'{"amount": 100}'
    assert res.response_payload["published"] is True


async def test_payload_subject_overrides_connection_default(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1", "subject": "pay.auth"})
    await a.connect()
    await a.execute("publish", {"subject": "pay.reversal", "amount": 5})
    assert fake.published[-1][0] == "pay.reversal"


async def test_control_keys_stripped_from_body(monkeypatch):
    """subject/timeout route the message; they must not leak into the body a
    subscriber sees (ADR-0006 catalog steps)."""
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1", "subject": "pay.auth"})
    await a.connect()
    await a.execute(
        "publish", {"subject": "pay.reversal", "timeout": 3, "pan": "4111", "amount": 5}
    )
    subject, data = fake.published[-1]
    assert subject == "pay.reversal"  # routed by control key
    assert codecs.decode(data, "json") == {"pan": "4111", "amount": 5}  # no subject/timeout


async def test_explicit_body_key_is_unwrapped(monkeypatch):
    """A step producing {subject, body:{…}} sends exactly the body, not the
    wrapper — this is the shape the catalog NATS steps emit."""
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1", "subject": "pay.auth"})
    await a.connect()
    await a.execute("publish", {"subject": "pay.auth", "body": {"decision": "APPROVE"}})
    _, data = fake.published[-1]
    assert codecs.decode(data, "json") == {"decision": "APPROVE"}


async def test_request_awaits_and_decodes_reply(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1", "subject": "pay.auth", "request_timeout_sec": 1.5})
    await a.connect()
    res = await a.execute("request", {"pan": "4111"})
    assert res.success
    assert fake.requests[-1][0] == "pay.auth"
    assert fake.requests[-1][2] == 1.5  # timeout threaded through
    assert res.response_payload["data"] == {"decision": "APPROVE"}


async def test_js_publish_returns_broker_ack(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1", "subject": "pay.events"})
    await a.connect()
    res = await a.execute("js_publish", {"event": "captured"})
    assert res.success
    assert res.response_payload["stream"] == "PAY"
    assert res.response_payload["seq"] == 7
    assert res.response_payload["acked"] is True
    assert fake.js_published  # went through JetStream, not core publish


async def test_missing_subject_is_a_stepresult_error(monkeypatch):
    _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1"})  # no default subject
    await a.connect()
    res = await a.execute("request", {"amount": 1})
    assert not res.success
    assert "subject" in (res.error or "")


async def test_iso8583_codec_round_trips_over_a_subject(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1", "subject": "iso.auth", "codec": "iso8583"})
    await a.connect()
    await a.execute("publish", {"mti": "0200", "de": {"3": "000000", "11": "000123"}})
    subject, data = fake.published[-1]
    assert subject == "iso.auth"
    # ISO 8583 ASCII on the wire, decodable back to the same DEs
    decoded = codecs.decode(data, "iso8583")
    assert decoded["mti"] == "0200"
    assert decoded["de"]["11"] == "000123"


async def test_health_check_flushes_and_never_raises(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1"})
    await a.connect()
    assert await a.health_check() is True
    assert fake.flushed


async def test_health_check_false_on_connect_error(monkeypatch):
    async def boom(servers, **opts):
        raise OSError("broker down")

    monkeypatch.setattr(nats_adapter, "_nats_connect", boom)
    a = NatsAdapter({"host": "nowhere"})
    assert await a.health_check() is False


async def test_disconnect_drains(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter({"host": "nats-1"})
    await a.connect()
    await a.disconnect()
    assert fake.drained


async def test_registry_maps_nats_to_nats_adapter():
    from worker.adapters.registry import ADAPTER_MAP

    # nats-py is installed in the test env; if it weren't, the key would be
    # absent (lazy registration) and that would be environmental, not a bug.
    assert ADAPTER_MAP.get("nats") is NatsAdapter


def test_servers_prefers_explicit_list():
    assert nats_adapter._servers_from({"servers": ["nats://a:4222", "nats://b:4223"]}) == [
        "nats://a:4222",
        "nats://b:4223",
    ]


def test_servers_builds_url_from_host_port():
    assert nats_adapter._servers_from({"host": "nats-1", "port": 4222}) == ["nats://nats-1:4222"]


def test_connect_opts_maps_auth():
    opts = nats_adapter._connect_opts(
        {"auth": {"token": "t", "nkey_seed": "SU...", "creds": "/x.creds"}}
    )
    assert opts["token"] == "t"
    assert opts["nkeys_seed_str"] == "SU..."
    assert opts["user_credentials"] == "/x.creds"


async def test_jetstream_creates_missing_stream_and_consumer(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter(
        {
            "host": "nats-1",
            "subject": "pay.events",
            "jetstream": {"stream": "PAY", "subjects": ["pay.>"], "durable": "pp", "ack_wait": 30},
        }
    )
    await a.connect()
    assert fake.stream_adds == ["PAY"]  # stream was created
    assert fake.consumer_adds == [("PAY", "pp")]  # durable consumer created
    assert a._created_stream == "PAY"  # ownership tracked
    assert a._created_consumer == ("PAY", "pp")


async def test_jetstream_stream_limits_bound_storage(monkeypatch):
    """A jetstream block with retention/limits is passed to add_stream so a
    producer-only load test can't fill storage forever (ADR-0006)."""
    fake = _patch(monkeypatch)
    a = NatsAdapter(
        {
            "host": "nats-1",
            "subject": "load.tx",
            "jetstream": {
                "stream": "PAYLOAD",
                "subjects": ["load.>"],
                "retention": "workqueue",
                "max_msgs": 1000,
                "max_age": 60,
            },
        }
    )
    await a.connect()
    kw = fake.stream_add_kwargs
    assert kw.get("max_msgs") == 1000
    assert kw.get("max_age") == 60 * 1e9  # seconds -> nanoseconds
    # retention maps to the nats-py enum (or falls back to the raw string)
    assert str(getattr(kw.get("retention"), "value", kw.get("retention"))) == "workqueue"


async def test_jetstream_leaves_preexisting_stream_untouched(monkeypatch):
    fake = _patch(monkeypatch)
    fake.streams["PAY"] = ["pay.>"]  # already exists (not ours)
    fake.consumers.add(("PAY", "pp"))
    a = NatsAdapter(
        {
            "host": "nats-1",
            "jetstream": {"stream": "PAY", "durable": "pp"},
        }
    )
    await a.connect()
    assert fake.stream_adds == []  # not created
    assert a._created_stream is None  # so not owned
    assert a._created_consumer is None


async def test_disconnect_deletes_only_created_consumer_not_stream(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter(
        {
            "host": "nats-1",
            "subject": "pay.events",
            "jetstream": {"stream": "PAY", "subjects": ["pay.>"], "durable": "pp"},
        }
    )
    await a.connect()
    await a.disconnect()
    assert fake.consumer_deletes == [("PAY", "pp")]  # our consumer removed
    assert fake.stream_deletes == []  # stream persists (durable)


async def test_disconnect_deletes_created_stream_when_opted_in(monkeypatch):
    fake = _patch(monkeypatch)
    a = NatsAdapter(
        {
            "host": "nats-1",
            "subject": "pay.events",
            "jetstream": {"stream": "EPHEMERAL", "subjects": ["e.>"], "delete_on_stop": True},
        }
    )
    await a.connect()
    await a.disconnect()
    assert fake.stream_deletes == ["EPHEMERAL"]  # opt-in cleanup


async def test_nats_as_load_target_via_registry(monkeypatch):
    """Load drives a NATS connection through the same registry.get + execute path
    the functional engine uses — request/reply TPS 'just works' as an adapter."""
    from worker.adapters.registry import AdapterRegistry

    fake = _patch(monkeypatch)
    reg = AdapterRegistry(
        {
            "adapters": {
                "issuer_bus": {"adapter": "nats", "host": "nats-1", "subject": "pay.auth"},
            }
        }
    )
    adapter = await reg.get("issuer_bus")
    sr = await adapter.execute("request", {"pan": "4111"})
    assert not sr.failed
    assert fake.requests[-1][0] == "pay.auth"


@pytest.mark.skipif(
    not os.environ.get("PAYPROBE_NATS_URL"),
    reason="opt-in: set PAYPROBE_NATS_URL to run against a real broker",
)
async def test_integration_request_reply_real_broker():
    import nats

    url = os.environ["PAYPROBE_NATS_URL"]
    server = await nats.connect(url)

    async def responder(msg):
        await server.publish(msg.reply, b'{"decision":"APPROVE"}')

    await server.subscribe("pay.auth.it", cb=responder)

    a = NatsAdapter({"servers": [url], "subject": "pay.auth.it"})
    await a.connect()
    res = await a.execute("request", {"pan": "4111"})
    assert res.success and res.response_payload["data"]["decision"] == "APPROVE"
    await a.disconnect()
    await server.drain()
