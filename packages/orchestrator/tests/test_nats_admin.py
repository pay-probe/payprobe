"""NATS cluster health + JetStream admin (ADR-0006): monitoring aggregation and
stream/consumer management against a fake broker + fake monitoring HTTP."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import nats_admin  # noqa: E402


# -- monitoring / health --------------------------------------------------------

def _fake_fetch(mapping):
    async def fetch(url):
        return mapping.get(url, (0, None))
    return fetch


async def test_monitoring_bases_derived_from_client_urls():
    bases = nats_admin.monitoring_bases(
        ["nats://nats-1:4222", "nats://nats-2:4223"], None)
    assert bases == ["http://nats-1:8222", "http://nats-2:8222"]


async def test_monitoring_bases_prefers_explicit():
    bases = nats_admin.monitoring_bases(
        ["nats://x:4222"], ["http://mon-1:8222"])
    assert bases == ["http://mon-1:8222"]


async def test_cluster_health_aggregates_nodes():
    fetch = _fake_fetch({
        "http://nats-1:8222/healthz": (200, {"status": "ok"}),
        "http://nats-1:8222/varz": (200, {"server_name": "nats-1", "version": "2.11",
                                          "connections": 3, "jetstream": {},
                                          "cluster": {"name": "payprobe"}}),
        "http://nats-1:8222/jsz": (200, {"streams": 2, "consumers": 4,
                                        "messages": 100, "bytes": 4096}),
        "http://nats-2:8222/healthz": (200, {"status": "ok"}),
        "http://nats-2:8222/varz": (200, {"server_name": "nats-2", "jetstream": {}}),
        "http://nats-2:8222/jsz": (200, {"streams": 2}),
    })
    out = await nats_admin.cluster_health(
        ["nats://nats-1:4222", "nats://nats-2:4223"], None, fetch)
    assert out["total"] == 2 and out["live"] == 2 and out["healthy"] is True
    assert out["jetstream"] is True
    n1 = out["nodes"][0]
    assert n1["server_name"] == "nats-1" and n1["cluster_name"] == "payprobe"
    assert n1["jetstream"]["streams"] == 2


async def test_cluster_health_marks_dead_node():
    fetch = _fake_fetch({
        "http://nats-1:8222/healthz": (200, {"status": "ok"}),
        # nats-2 not in the map -> (0, None) -> unhealthy
    })
    out = await nats_admin.cluster_health(
        ["nats://nats-1:4222", "nats://nats-2:4223"], None, fetch)
    assert out["live"] == 1 and out["total"] == 2 and out["healthy"] is False


# -- JetStream admin (fake broker) ---------------------------------------------

class _Cfg:
    def __init__(self, name, subjects, durable=None):
        self.name = name
        self.subjects = subjects
        self.durable_name = durable


class _State:
    def __init__(self, messages=0, bytes=0, consumer_count=0):
        self.messages = messages
        self.bytes = bytes
        self.consumer_count = consumer_count


class _StreamInfo:
    def __init__(self, name, subjects, **st):
        self.config = _Cfg(name, subjects)
        self.state = _State(**st)


class _ConsumerInfo:
    def __init__(self, name, durable):
        self.name = name
        self.config = _Cfg(name, [], durable=durable)


class _FakeJS:
    def __init__(self, client):
        self.client = client

    async def streams_info(self):
        return self.client.streams

    async def add_stream(self, name, subjects):
        info = _StreamInfo(name, subjects)
        self.client.streams.append(info)
        self.client.added.append((name, tuple(subjects)))
        return info

    async def delete_stream(self, name):
        self.client.deleted.append(name)

    async def consumers_info(self, stream):
        return self.client.consumers

    async def delete_consumer(self, stream, durable):
        self.client.consumer_deletes.append((stream, durable))


class _FakeNats:
    def __init__(self):
        self.streams = [_StreamInfo("PAY", ["pay.>"], messages=10, bytes=512,
                                    consumer_count=1)]
        self.consumers = [_ConsumerInfo("pp", "pp")]
        self.added = []
        self.deleted = []
        self.consumer_deletes = []
        self.drained = False

    def jetstream(self):
        return _FakeJS(self)

    async def drain(self):
        self.drained = True

    async def close(self):
        pass


def _fake_connect():
    fake = _FakeNats()

    async def connect(servers, **opts):
        fake.opts = opts
        return fake

    return fake, connect


async def test_list_streams_normalizes_rows():
    fake, connect = _fake_connect()
    out = await nats_admin.list_streams(["nats://x:4222"], None, connect)
    assert out["streams"] == [
        {"name": "PAY", "subjects": ["pay.>"], "messages": 10, "bytes": 512,
         "consumers": 1}]
    assert fake.drained  # connection always drained


async def test_create_stream_requires_name_and_subjects():
    _, connect = _fake_connect()
    with pytest.raises(ValueError):
        await nats_admin.create_stream(["nats://x:4222"], None, connect,
                                       name="", subjects=[])


async def test_create_stream_adds_and_returns_view():
    fake, connect = _fake_connect()
    out = await nats_admin.create_stream(
        ["nats://x:4222"], None, connect, name="EVT", subjects=["evt.>"])
    assert out["created"] is True and out["stream"]["name"] == "EVT"
    assert fake.added == [("EVT", ("evt.>",))]


async def test_delete_stream_and_consumer():
    fake, connect = _fake_connect()
    d = await nats_admin.delete_stream(["nats://x:4222"], None, connect, name="PAY")
    assert d == {"deleted": True, "stream": "PAY"} and fake.deleted == ["PAY"]

    fake2, connect2 = _fake_connect()
    dc = await nats_admin.delete_consumer(
        ["nats://x:4222"], None, connect2, stream="PAY", durable="pp")
    assert dc["deleted"] is True and fake2.consumer_deletes == [("PAY", "pp")]


async def test_auth_maps_into_connect_opts():
    opts = nats_admin._connect_opts({"token": "t", "nkey_seed": "SU", "creds": "/c"})
    assert opts["token"] == "t" and opts["nkeys_seed_str"] == "SU"
    assert opts["user_credentials"] == "/c"


async def test_list_consumers_rows():
    _, connect = _fake_connect()
    out = await nats_admin.list_consumers(["nats://x:4222"], None, connect, stream="PAY")
    assert out == {"stream": "PAY", "consumers": [{"name": "pp", "durable": "pp"}]}
