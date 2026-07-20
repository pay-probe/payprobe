"""POST /showcase/install — the portal's one-click demo network (UX onboarding).

Verifies the endpoint drives the right scenario-service calls (connections,
flows, group, driver scenario, network) from the shared spec, creates the
payShield simulator locally, wires the resolved ids into the network, and is
idempotent — all with the sibling HTTP mocked (no live scenario-service)."""
import os

os.environ["DISABLE_SCHEDULER"] = "1"

import pytest  # noqa: E402

from orchestrator.api import main as m  # noqa: E402
from orchestrator.api import showcase_spec as spec  # noqa: E402


class FakeScenarioService:
    """Minimal in-memory stand-in for scenario-service over the HTTP helpers."""

    def __init__(self) -> None:
        self.put: list[tuple[str, dict]] = []      # (path, body)
        self.scenarios: dict[str, dict] = {}
        self.networks: dict[str, dict] = {}
        self._n = 0

    def _path(self, url: str) -> str:
        return url.replace(m.SCENARIO_API_URL, "")

    async def get(self, url: str):
        p = self._path(url)
        if p == "/scenarios":
            return [{"id": k, "name": v.get("name")} for k, v in self.scenarios.items()]
        raise AssertionError(f"unexpected GET {p}")

    async def post(self, url: str, body: dict):
        p = self._path(url)
        if p == "/scenarios":
            self._n += 1
            sid = f"scn-{self._n}"
            self.scenarios[sid] = {"id": sid, **(body.get("scenario") or {})}
            return self.scenarios[sid]
        raise AssertionError(f"unexpected POST {p}")

    async def req(self, method: str, url: str, body: dict | None = None):
        p = self._path(url)
        self.put.append((f"{method} {p}", body or {}))
        if p.startswith("/network-flows/"):
            self.networks[p.rsplit("/", 1)[-1]] = body or {}
        return None

    def paths(self) -> list[str]:
        return [p for p, _ in self.put]


@pytest.fixture()
def fake(monkeypatch):
    fb = FakeScenarioService()
    monkeypatch.setattr(m, "_http_get_json", fb.get)
    monkeypatch.setattr(m, "_http_post_json", fb.post)
    monkeypatch.setattr(m, "_http_json", fb.req)
    # a clean simulator store per test
    from orchestrator.api.simulator_store import SimulatorStore
    monkeypatch.setattr(m, "simulator_store", SimulatorStore(":memory:"))
    return fb


async def test_install_builds_every_artifact_and_wires_resolved_ids(fake):
    out = await m.install_showcase(m.ShowcaseInstall(start=False))

    assert out["network_flow_id"] == spec.NET_ID
    assert out["started"] is False
    # connections + flows + group all PUT by chosen id
    for name in spec.CONNS:
        assert f"PUT /connections/{name}" in fake.paths()
    assert f"PUT /participant-flows/{spec.ISSUER_FLOW}" in fake.paths()
    assert f"PUT /participant-flows/{spec.SWITCH_FLOW}" in fake.paths()
    assert f"PUT /participant-groups/{spec.GROUP_ID}" in fake.paths()

    # driver scenario created (server id), simulator created locally
    assert out["driver_id"] == "scn-1"
    assert m.simulator_store.get(out["simulator_id"])["label"] == spec.SIM_LABEL

    # the saved network references the RESOLVED driver + sim ids
    net = fake.networks[spec.NET_ID]
    cfg = {n["id"]: n["config"] for n in net["nodes"]}
    assert cfg["driver"]["scenario_id"] == "scn-1"
    assert cfg["hsm"]["simulator_id"] == out["simulator_id"]


async def test_install_is_idempotent(fake):
    first = await m.install_showcase(m.ShowcaseInstall())
    second = await m.install_showcase(m.ShowcaseInstall())
    # no duplicate driver scenario, no duplicate simulator
    assert first["driver_id"] == second["driver_id"]
    assert first["simulator_id"] == second["simulator_id"]
    assert len(m.simulator_store.list()) == 1
    assert len(fake.scenarios) == 1


async def test_install_can_start_the_network(fake, monkeypatch):
    started = {}

    async def fake_start(nid):
        started["nid"] = nid
        return {"id": "run-1", "health": {"ready": True}}

    monkeypatch.setattr(m, "start_network_flow", fake_start)
    out = await m.install_showcase(m.ShowcaseInstall(start=True))
    assert out["started"] is True
    assert started["nid"] == spec.NET_ID
    assert out["run"]["id"] == "run-1"
