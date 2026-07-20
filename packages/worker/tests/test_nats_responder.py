"""NatsResponder (ADR-0006): a rules-driven NATS subscriber that decodes each
message, matches a rule on subject + body, and answers request/reply messages on
their inbox. Binds no port; chaos works the same as the other responders. The
broker is faked — no nats-py server required.
"""

import pytest

# nats-py is an optional dep (ADR-0006): its absence skips this suite.
pytest.importorskip("nats")

import worker.adapters.nats.responder as nats_responder
from worker.adapters.nats.responder import NatsResponder
from worker.adapters.nats import codecs


class FakeSub:
    def __init__(self, subject, queue, cb):
        self.subject = subject
        self.queue = queue
        self.cb = cb
        self.unsubscribed = False

    async def unsubscribe(self):
        self.unsubscribed = True


class FakeNats:
    def __init__(self):
        self.subs: list[FakeSub] = []
        self.published: list[tuple[str, bytes]] = []
        self.drained = False

    async def subscribe(self, subject, queue="", cb=None):
        sub = FakeSub(subject, queue, cb)
        self.subs.append(sub)
        return sub

    async def publish(self, subject, data, **kw):
        self.published.append((subject, data))

    async def drain(self):
        self.drained = True

    async def close(self):
        pass


class FakeMsg:
    def __init__(self, subject, data, reply=""):
        self.subject = subject
        self.data = data
        self.reply = reply


def _patch(monkeypatch):
    fake = FakeNats()

    async def fake_connect(servers, **opts):
        return fake

    monkeypatch.setattr(nats_responder, "_nats_connect", fake_connect)
    return fake


async def test_start_subscribes_to_each_subject_in_queue_group(monkeypatch):
    fake = _patch(monkeypatch)
    r = NatsResponder(
        {
            "host": "nats-1",
            "subjects": ["pay.auth", "pay.reversal"],
            "queue_group": "issuers",
        }
    )
    await r.start()
    assert r.port is None  # binds no port
    assert [s.subject for s in fake.subs] == ["pay.auth", "pay.reversal"]
    assert all(s.queue == "issuers" for s in fake.subs)


async def test_start_with_no_subjects_errors(monkeypatch):
    import pytest

    _patch(monkeypatch)
    r = NatsResponder({"host": "nats-1"})
    with pytest.raises(ValueError, match="no subjects"):
        await r.start()


async def test_request_reply_publishes_to_inbox(monkeypatch):
    fake = _patch(monkeypatch)
    r = NatsResponder(
        {
            "host": "nats-1",
            "subject": "pay.auth",
            "default": {"json": {"decision": "APPROVE"}},
        }
    )
    await r.start()
    await fake.subs[0].cb(FakeMsg("pay.auth", b'{"amount": 5}', reply="_INBOX.x"))
    assert fake.published, "expected a reply on the inbox"
    subject, data = fake.published[-1]
    assert subject == "_INBOX.x"
    assert codecs.decode(data, "json") == {"decision": "APPROVE"}
    assert r.by_mti["pay.auth"] == 1
    assert r.stats()["received"] == 1


async def test_rule_matches_on_subject_and_body(monkeypatch):
    fake = _patch(monkeypatch)
    r = NatsResponder(
        {
            "host": "nats-1",
            "subject": "pay.auth",
            "rules": [
                {
                    "when": {"body": {"amount": {"gte": 1000}}},
                    "respond": {"json": {"decision": "DECLINE"}},
                },
            ],
            "default": {"json": {"decision": "APPROVE"}},
        }
    )
    await r.start()
    await fake.subs[0].cb(FakeMsg("pay.auth", b'{"amount": 5000}', reply="_INBOX.y"))
    assert codecs.decode(fake.published[-1][1], "json") == {"decision": "DECLINE"}


async def test_fire_and_forget_records_but_does_not_reply(monkeypatch):
    fake = _patch(monkeypatch)
    r = NatsResponder({"host": "nats-1", "subject": "pay.events"})
    await r.start()
    # no reply inbox -> publish-only; nothing to answer
    await fake.subs[0].cb(FakeMsg("pay.events", b'{"e": 1}', reply=""))
    assert not fake.published
    assert r.stats()["received"] == 1


async def test_chaos_drop_publishes_nothing_and_counts_fault(monkeypatch):
    fake = _patch(monkeypatch)
    r = NatsResponder(
        {
            "host": "nats-1",
            "subject": "pay.auth",
            "chaos": {"drop": 1.0, "seed": 1},
        }
    )
    await r.start()
    await fake.subs[0].cb(FakeMsg("pay.auth", b'{"amount": 1}', reply="_INBOX.z"))
    assert not fake.published  # dropped
    assert r.faults == 1
    assert r.by_rc.get("(drop)") == 1


async def test_iso8583_codec_reply(monkeypatch):
    fake = _patch(monkeypatch)
    r = NatsResponder(
        {
            "host": "nats-1",
            "subject": "iso.auth",
            "codec": "iso8583",
            "default": {"json": {"mti": "0210", "de": {"39": "00"}}},
        }
    )
    await r.start()
    req = codecs.encode({"mti": "0200", "de": {"11": "000123"}}, "iso8583")
    await fake.subs[0].cb(FakeMsg("iso.auth", req, reply="_INBOX.i"))
    reply = codecs.decode(fake.published[-1][1], "iso8583")
    assert reply["mti"] == "0210"
    assert reply["de"]["39"] == "00"


async def test_set_chaos_swaps_live(monkeypatch):
    _patch(monkeypatch)
    r = NatsResponder({"host": "nats-1", "subject": "pay.auth"})
    r.set_chaos({"drop": 0.5})
    assert r.global_chaos == {"drop": 0.5}
    r.set_chaos(None)
    assert r.global_chaos == {}


async def test_stop_unsubscribes_and_drains(monkeypatch):
    fake = _patch(monkeypatch)
    r = NatsResponder({"host": "nats-1", "subject": "pay.auth"})
    await r.start()
    await r.stop()
    assert all(s.unsubscribed for s in fake.subs)
    assert fake.drained


class _FakeJSSub:
    async def unsubscribe(self):
        pass


class _FakeMsg:
    def __init__(self, subject, data):
        self.subject = subject
        self.data = data
        self.reply = ""
        self.acked = False

    async def ack(self):
        self.acked = True


class _FakeJS:
    def __init__(self, client):
        self.client = client

    async def subscribe(self, subject, stream=None, durable=None, cb=None, manual_ack=None, **kw):
        self.client.js_subs.append(
            {
                "subject": subject,
                "stream": stream,
                "durable": durable,
                "manual_ack": manual_ack,
                "cb": cb,
            }
        )
        return _FakeJSSub()


async def test_jetstream_consumer_subscribes_durable_and_acks(monkeypatch):
    """A responder with a jetstream block consumes as a durable JS consumer and
    ACKs each message — draining the stream instead of piling up (ADR-0006)."""
    fake = _patch(monkeypatch)
    fake.js_subs = []
    fake.jetstream = lambda: _FakeJS(fake)

    r = NatsResponder(
        {
            "host": "nats-1",
            "subjects": ["load.tx"],
            "jetstream": {"stream": "PAYLOAD", "durable": "drainer"},
        }
    )
    await r.start()
    assert len(fake.js_subs) == 1
    js = fake.js_subs[0]
    assert js["stream"] == "PAYLOAD" and js["durable"] == "drainer"
    assert js["manual_ack"] is True

    # deliver a stream message → handled (recorded) AND acked (drains the stream)
    msg = _FakeMsg("load.tx", b'{"amount": 4200}')
    await r._handle_js(msg)
    assert msg.acked is True
    assert r.stats()["received"] == 1


async def test_peers_reports_subjects(monkeypatch):
    _patch(monkeypatch)
    r = NatsResponder({"host": "nats-1", "subjects": ["a", "b"], "queue_group": "g"})
    await r.start()
    peers = r.peers()
    assert {p["peer"] for p in peers} == {"subject:a@g", "subject:b@g"}
