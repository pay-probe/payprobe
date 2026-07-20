"""Playground samples (ADR-0007 follow-up): the ready-made composer examples.

The module promises two things a client relies on — every crypto operation has
a sample, and every crypto sample *fires green as-is* — and the family resolver
picks the right wire set. These tests enforce all three so the promise can't
rot as ``OPERATIONS`` or the adapters evolve.
"""
import pytest

from orchestrator.api.playground_samples import (
    CRYPTO_SAMPLES,
    WIRE_SAMPLES,
    sample_family,
    samples_catalog,
)


# -- crypto samples: complete + green -----------------------------------------

def test_crypto_samples_cover_every_operation_exactly():
    from worker.engine.crypto_tools import OPERATIONS

    ops = set(OPERATIONS)
    samp = set(CRYPTO_SAMPLES)
    assert samp == ops, {
        "missing_sample": sorted(ops - samp),
        "orphan_sample": sorted(samp - ops),
    }


def test_every_crypto_sample_fires_green():
    """The headline promise: a fresh operator can click any function sample and
    it just runs. Needs pycryptodome — environmental, so skip if absent."""
    pytest.importorskip("Crypto")
    from worker.engine.crypto_tools import run_crypto

    failures = {
        op: res["error"]
        for op, params in CRYPTO_SAMPLES.items()
        if "error" in (res := run_crypto(op, dict(params)))
    }
    assert not failures, failures


# -- family resolution --------------------------------------------------------

def test_sample_family_tcp_is_protocol_driven():
    # ONLY the universal tcp adapter reads its protocol (mirrors _conn_type_key):
    # a tcp connection speaking visa draws the VISA set, not iso8583.
    assert sample_family(protocol="visa", adapter="tcp") == "visa"
    assert sample_family(protocol="header_echo", adapter="tcp") == "header_echo"
    assert sample_family(adapter="tcp") == "iso8583"          # no protocol → ISO


def test_non_tcp_adapter_ignores_defaulted_protocol():
    # THE BUG: a NATS/HTTP/gRPC connection carries protocol="iso8583" (the
    # ConnectionDraft default) — the adapter must win, not the stale protocol.
    assert sample_family(protocol="iso8583", adapter="nats") == "nats"
    assert sample_family(protocol="iso8583", adapter="http") == "http"
    assert sample_family(protocol="iso8583", adapter="grpc") == "grpc"
    assert sample_family(protocol="iso8583", adapter="cybersource") == "cybersource"
    assert sample_family(protocol="iso8583", adapter="payshield") == "payshield"


def test_sample_family_folds_aliases():
    assert sample_family(adapter="switch") == "iso8583"
    assert sample_family(adapter="hsm_client") == "payshield"
    assert sample_family(adapter="hsm") == "header_echo"      # transport link
    # a simulator carries only a protocol (no adapter) — protocol is authoritative
    assert sample_family(protocol="nats") == "nats"
    assert sample_family(protocol="cybersource") == "cybersource"
    assert sample_family(protocol="header_echo") == "header_echo"


def test_sample_family_none_when_unknown():
    # honest: no chips rather than wrong chips for an unmapped shape.
    assert sample_family(protocol="smtp", adapter="carrier-pigeon") is None
    assert sample_family() is None


# -- catalog shape ------------------------------------------------------------

def test_samples_catalog_shape_matches_client_contract():
    cat = samples_catalog()
    assert set(cat) == {"wire", "functions"}
    assert cat["wire"] is not None and "iso8583" in cat["wire"]
    crypto = cat["functions"]["crypto"]
    # one composer-shaped entry per operation, sorted, action == op name.
    assert [s["name"] for s in crypto] == sorted(CRYPTO_SAMPLES)
    for s in crypto:
        assert set(s) >= {"name", "action", "payload"}
        assert s["action"] == s["name"]


def test_wire_samples_are_composer_shaped():
    for family, rows in WIRE_SAMPLES.items():
        assert rows, f"{family} has no samples"
        for s in rows:
            assert s["action"], f"{family}/{s.get('name')} has no action"
            assert isinstance(s["payload"], dict)
