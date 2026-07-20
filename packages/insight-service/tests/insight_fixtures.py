"""Shared test helpers: a fake orchestrator with canned run history.

Offline, fake-first (the repo's test culture §9): the reader's ``fetch`` is a
dict lookup, no sockets anywhere. Kept OUT of conftest.py under a unique
basename so cross-package collection never sees duplicate module names.
"""
from __future__ import annotations




def _step(step_id: str, status: str, error: str | None = None,
          duration_ms: int = 40, target: str = "issuer",
          action: str = "send_message") -> dict:
    return {"step_id": step_id, "target": target, "action": action,
            "status": status, "duration_ms": duration_ms, "error": error,
            "assertions": []}


def _run(run_id: str, created_at: str, scenarios: list[dict],
         environment: str = "staging") -> dict:
    return {
        "id": run_id, "status": "failed"
        if any(s["status"] == "failed" for s in scenarios) else "passed",
        "environment": environment, "label": "regression",
        "created_at": created_at, "summary": {"scenarios": scenarios},
    }


def _scenario(sid: str, status: str, steps: list[dict]) -> dict:
    return {"scenario_id": sid, "name": sid.replace("-", " "),
            "status": status, "steps": steps}


TIMEOUT = "read timed out after 5000 ms connecting to issuer-a:8583"
REFUSED = "connect to 10.1.2.3:8583 failed: connection refused"
DECLINE = "assertion failed: expected response_code 00 got 05"
WEIRD = "payShield stream error 0xBEEF: handler is closed (conn 48213)"


def build_history() -> list[dict]:
    """8 runs: sc-stable always passes; sc-flaky alternates (timeouts);
    sc-broken fails every run (refused); sc-decline fails twice with an
    assertion mismatch then recovers; one novel payShield failure."""
    runs = []
    for i in range(8):
        t = f"2026-07-{i + 1:02d}T10:00:00+00:00"
        flaky_fails = i % 2 == 1
        scenarios = [
            _scenario("sc-stable", "passed",
                      [_step("s1", "passed", duration_ms=40 + i * 8)]),
            _scenario("sc-flaky",
                      "failed" if flaky_fails else "passed",
                      [_step("s1", "failed" if flaky_fails else "passed",
                             TIMEOUT if flaky_fails else None)]),
            _scenario("sc-broken", "failed",
                      [_step("s1", "failed", REFUSED)]),
            _scenario("sc-decline",
                      "failed" if i in (2, 3) else "passed",
                      [_step("s1", "failed" if i in (2, 3) else "passed",
                             DECLINE if i in (2, 3) else None)]),
        ]
        if i == 7:
            scenarios.append(
                _scenario("sc-hsm", "failed", [_step("s1", "failed", WEIRD,
                                                     target="hsm")]))
        runs.append(_run(f"run-{i + 1}", t, scenarios))
    return runs


class FakeOrchestrator:
    def __init__(self, runs: list[dict],
                 load_runs: list[dict] | None = None) -> None:
        self.runs = {r["id"]: r for r in runs}
        self.load_runs = {r["id"]: r for r in (load_runs or [])}

    def fetch(self, path: str):
        if path == "/runs":
            return [{"id": r["id"], "status": r["status"],
                     "created_at": r["created_at"]} for r in self.runs.values()]
        if path == "/load-runs":
            return [{"id": r["id"], "status": r.get("status", "completed"),
                     "created_at": r.get("created_at", "")}
                    for r in self.load_runs.values()]
        if path.startswith("/load-runs/"):
            return self.load_runs.get(path.split("/load-runs/")[1])
        if path.startswith("/runs/"):
            return self.runs.get(path.split("/runs/")[1])
        raise AssertionError(f"unexpected path {path}")
