"""Hardening of the code-node runner: network sandbox + resource limits."""

import asyncio

import pytest

from worker.engine import code_runner
from worker.engine.code_runner import run_code


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    code_runner._reset_sandbox_cache()
    yield
    code_runner._reset_sandbox_cache()


def _run(**kw):
    return asyncio.run(run_code(**kw))


def test_normal_python_still_runs_with_hardening():
    """Sandboxing/rlimits must not break ordinary snippets."""
    res = _run(
        language="python", code="return {'v': context['x'] + 1}", context={"x": 41}, inputs={}
    )
    assert res.ok
    assert res.output == {"v": 42}


def test_off_mode_disables_isolation(monkeypatch):
    monkeypatch.setenv("PAYPROBE_CODE_SANDBOX", "off")
    code_runner._reset_sandbox_cache()
    assert code_runner.network_isolation_active() is False


def test_strict_mode_refuses_when_unshare_absent(monkeypatch):
    """strict + no unshare ⇒ refuse to run rather than execute with egress."""
    monkeypatch.setenv("PAYPROBE_CODE_SANDBOX", "strict")
    monkeypatch.setattr(
        code_runner.shutil, "which", lambda name: None if name == "unshare" else "/usr/bin/" + name
    )
    code_runner._reset_sandbox_cache()
    res = _run(language="python", code="return {'ok': True}", context={}, inputs={})
    assert not res.ok
    assert "sandbox required" in (res.error or "")


def test_auto_mode_runs_even_without_unshare(monkeypatch):
    """auto + no unshare ⇒ still runs (best-effort), just without isolation."""
    monkeypatch.setenv("PAYPROBE_CODE_SANDBOX", "auto")
    monkeypatch.setattr(
        code_runner.shutil, "which", lambda name: None if name == "unshare" else "/usr/bin/" + name
    )
    code_runner._reset_sandbox_cache()
    assert code_runner.network_isolation_active() is False
    res = _run(language="python", code="return {'v': 7}", context={}, inputs={})
    assert res.ok and res.output == {"v": 7}


def test_isolation_active_when_unshare_works():
    """On a host where unshare creates a netns, isolation reports active and a
    snippet has no network. Skipped where namespaces aren't permitted."""
    code_runner._reset_sandbox_cache()
    if not code_runner.network_isolation_active():
        pytest.skip("network namespaces not available in this environment")
    res = _run(
        language="python",
        code=(
            "import socket\n"
            "socket.setdefaulttimeout(2)\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53))\n"
            "    return {'net': 'open'}\n"
            "except OSError:\n"
            "    return {'net': 'blocked'}\n"
        ),
        context={},
        inputs={},
    )
    assert res.ok
    assert res.output == {"net": "blocked"}
