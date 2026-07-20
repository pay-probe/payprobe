"""Tests for dynamic worker provisioning.

Covers:
* the backend-selection heuristic (`make_provisioner`: none / local / compose / auto)
* NullProvisioner — no-op scale that carries the manual command (custom path)
* LocalSubprocessProvisioner — declarative scale up/down + reaping exited workers
  (process spawning is faked so the suite needs no Redis or worker image)
* the manual command renders the run id + Redis URL
* LoadCoordinator.scale drives the provisioner and reports its status
"""
from __future__ import annotations

import asyncio

import pytest

from orchestrator.api.worker_provisioner import (
    ComposeProvisioner,
    DockerProvisioner,
    LocalSubprocessProvisioner,
    NullProvisioner,
    make_provisioner,
    manual_command,
)
from orchestrator.api.load_coordinator import LoadCoordinator
from worker.engine.load import InMemoryLoadBus, LoadProfile


# -- factory -----------------------------------------------------------------


def test_make_provisioner_none_is_unavailable():
    p = make_provisioner("none")
    assert p.kind == "none"
    assert p.available is False


def test_make_provisioner_local_needs_redis():
    assert make_provisioner("local", redis_url="redis://x:6379").available is True
    # auto with no Redis falls back to a Null provisioner (in-process covers it)
    auto = make_provisioner("auto", redis_url=None)
    assert auto.available is False


def test_make_provisioner_compose_unavailable_without_config(monkeypatch):
    # no compose file configured -> not usable, with a helpful detail
    monkeypatch.delenv("PAYPROBE_COMPOSE_FILE", raising=False)
    p = make_provisioner("compose", redis_url="redis://x:6379")
    assert p.kind == "compose"
    assert p.available is False
    assert p.detail  # explains what to fix


def test_make_provisioner_docker_unavailable_without_image(monkeypatch):
    monkeypatch.delenv("PAYPROBE_WORKER_IMAGE", raising=False)
    p = make_provisioner("docker", redis_url="redis://x:6379")
    assert p.kind == "docker"
    # unavailable either because docker CLI is absent or no image configured
    assert p.available is False
    assert p.detail


def test_make_provisioner_auto_prefers_docker_when_image_set(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.api.worker_provisioner.shutil.which", lambda _x: "/usr/bin/docker")
    monkeypatch.setenv("PAYPROBE_WORKER_IMAGE", "payprobe/worker")
    p = make_provisioner("auto", redis_url="redis://x:6379")
    assert p.kind == "docker"
    assert p.available is True


# -- DockerProvisioner -------------------------------------------------------


class _FakeDockerCli:
    """Stands in for the `docker` CLI: tracks containers in memory so scale
    up/down can be exercised without a daemon."""

    def __init__(self) -> None:
        self.containers: set[str] = set()
        self.runs: list[list[str]] = []

    async def __call__(self, *args: str):
        self.runs.append(list(args))
        a = list(args)
        if a[:2] == ["ps", "-q"]:
            prefix = a[a.index("--filter") + 1].split("=", 1)[1]
            hits = [c for c in self.containers if c.startswith(prefix)]
            return 0, "\n".join(f"id-{c}" for c in hits), ""
        if a[:2] == ["ps", "-a"]:
            prefix = a[a.index("--filter") + 1].split("=", 1)[1]
            hits = [c for c in self.containers if c.startswith(prefix)]
            return 0, "\n".join(sorted(hits)), ""
        if a[0] in ("run", "compose"):
            name = a[a.index("--name") + 1]
            self.containers.add(name)
            return 0, f"id-{name}", ""
        if a[:2] == ["rm", "-f"]:
            self.containers.discard(a[2])
            return 0, a[2], ""
        return 1, "", f"unhandled: {a}"


def _docker_with_fake_cli() -> tuple[DockerProvisioner, _FakeDockerCli]:
    p = DockerProvisioner(image="payprobe/worker", network="payprobe_default",
                          redis_url="redis://redis:6379")
    p.docker = "/usr/bin/docker"
    p.available, p.detail = True, ""
    fake = _FakeDockerCli()
    p._run = fake  # type: ignore[assignment]
    return p, fake


def test_docker_start_args_is_a_fresh_container():
    p = DockerProvisioner(image="payprobe/worker", network="net0",
                          redis_url="redis://redis:6379")
    args = p._start_args("run-9", "payprobe-lw-run-9-0")
    # a detached, named container on the redis network, bound to the run,
    # overriding the batch entrypoint to the load worker. NOT --rm: a crashed
    # worker must persist as "exited" so the provisioner can read its logs and
    # surface why it died (reaped by _prune_exited / teardown instead).
    assert args[0] == "run"
    assert "-d" in args and "--rm" not in args
    assert ["--name", "payprobe-lw-run-9-0"] == args[args.index("--name"):args.index("--name") + 2]
    assert ["--network", "net0"] == args[args.index("--network"):args.index("--network") + 2]
    assert "LOAD_RUN_ID=run-9" in args
    assert "REDIS_URL=redis://redis:6379" in args
    assert args[-5:] == ["--entrypoint", "python", "payprobe/worker",
                         "-m", "worker.load_worker"]


def test_docker_scale_starts_and_removes_containers():
    p, fake = _docker_with_fake_cli()

    async def go():
        r = await p.scale("run-1", 3)
        assert r == {"kind": "docker", "available": True, "requested": 3, "running": 3}
        assert len(fake.containers) == 3
        # every spawn was a `docker run`
        assert sum(1 for c in fake.runs if c and c[0] == "run") == 3
        assert await p.count("run-1") == 3
        # scale down removes containers
        r = await p.scale("run-1", 1)
        assert r["running"] == 1
        assert len(fake.containers) == 1
        # teardown removes the rest
        await p.teardown("run-1")
        assert len(fake.containers) == 0

    asyncio.run(go())


def test_docker_scale_indexes_dont_collide_on_rescale():
    p, fake = _docker_with_fake_cli()

    async def go():
        await p.scale("run-1", 2)  # -> -0, -1
        await p.scale("run-1", 1)  # removes -1, leaves -0
        await p.scale("run-1", 2)  # must add a new index, not reuse -0
        assert len(fake.containers) == 2

    asyncio.run(go())


# -- manual command ----------------------------------------------------------


def test_manual_command_has_run_id_and_redis():
    cmd = manual_command("run-123", "redis://r:6379")
    assert "LOAD_RUN_ID=run-123" in cmd
    assert "REDIS_URL=redis://r:6379" in cmd
    assert "python -m worker.load_worker" in cmd


# -- NullProvisioner ---------------------------------------------------------


def test_null_scale_returns_manual_notice():
    p = NullProvisioner()
    res = asyncio.run(p.scale("run-1", 4))
    assert res["available"] is False
    assert "manually" in res["notice"].lower()
    assert "LOAD_RUN_ID=run-1" in res["notice"]


# -- LocalSubprocessProvisioner ----------------------------------------------


class _FakeProc:
    _pids = iter(range(1000, 100000))

    def __init__(self) -> None:
        self.pid = next(self._pids)
        self.returncode = None

    def terminate(self) -> None:
        self.returncode = -15


def _local_with_fake_spawn() -> LocalSubprocessProvisioner:
    p = LocalSubprocessProvisioner(redis_url="redis://x:6379")

    async def _fake_spawn(run_id: str):
        return _FakeProc()

    p._spawn_one = _fake_spawn  # type: ignore[assignment]
    return p


def test_local_scale_up_then_down():
    p = _local_with_fake_spawn()

    async def go():
        r = await p.scale("run-1", 3)
        assert r == {"kind": "local", "available": True, "requested": 3, "running": 3}
        assert await p.count("run-1") == 3
        # declarative: scaling to 1 terminates two
        r = await p.scale("run-1", 1)
        assert r["running"] == 1
        assert await p.count("run-1") == 1
        # teardown reaps the rest
        await p.teardown("run-1")
        assert await p.count("run-1") == 0

    asyncio.run(go())


def test_local_reaps_exited_workers():
    p = _local_with_fake_spawn()

    async def go():
        await p.scale("run-1", 2)
        # simulate one worker exiting on its own
        p._procs["run-1"][0].returncode = 0
        assert await p.count("run-1") == 1
        # scaling back to 2 replaces the dead one
        r = await p.scale("run-1", 2)
        assert r["running"] == 2

    asyncio.run(go())


def test_local_unavailable_without_redis():
    p = LocalSubprocessProvisioner(redis_url=None)
    assert p.available is False
    res = asyncio.run(p.scale("run-1", 2))
    assert res["available"] is False
    assert res["running"] == 0


# -- coordinator integration -------------------------------------------------


class _RecordingProvisioner(NullProvisioner):
    kind = "local"
    available = True

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, int]] = []
        self._count = 0

    async def scale(self, run_id: str, desired: int) -> dict:
        self.calls.append((run_id, desired))
        self._count = desired
        return {"kind": self.kind, "available": True,
                "requested": desired, "running": desired}

    async def count(self, run_id: str) -> int:
        return self._count


def test_coordinator_scale_drives_provisioner():
    prov = _RecordingProvisioner()
    coord = LoadCoordinator(InMemoryLoadBus(), backbone=None, provisioner=prov)

    async def go():
        profile = LoadProfile(type="steady", duration_s=10, workers=2, target_tps=100)
        # register a live run without spawning the full pipeline
        from orchestrator.api.load_coordinator import LoadRun
        coord.runs["run-1"] = LoadRun("run-1", profile)
        res = await coord.scale("run-1", 5)
        assert res["running"] == 5
        assert ("run-1", 5) in prov.calls
        # the in-memory profile reflects the new fleet size
        assert coord.runs["run-1"].profile.workers == 5

    asyncio.run(go())


def test_coordinator_scale_unknown_run_raises():
    coord = LoadCoordinator(InMemoryLoadBus(), backbone=None, provisioner=_RecordingProvisioner())
    with pytest.raises(KeyError):
        asyncio.run(coord.scale("nope", 2))


def test_coordinator_scale_without_provisioner_returns_manual():
    coord = LoadCoordinator(InMemoryLoadBus(), backbone=None, provisioner=None)

    async def go():
        from orchestrator.api.load_coordinator import LoadRun
        profile = LoadProfile(type="steady", duration_s=10, workers=1, target_tps=100)
        coord.runs["run-1"] = LoadRun("run-1", profile)
        res = await coord.scale("run-1", 3)
        assert res["available"] is False
        assert "manually" in res["notice"].lower()

    asyncio.run(go())


def test_scale_up_publishes_new_shards_and_retunes_fleet():
    """Scaling a live run up must push shards for the new workers (or they find
    an empty queue, idle out and exit — 'the fleet is 4 but the run uses 2')
    and re-slice existing workers so the fleet still sums to the target."""
    prov = _RecordingProvisioner()
    bus = InMemoryLoadBus()
    coord = LoadCoordinator(bus, backbone=None, provisioner=prov)

    async def go():
        from orchestrator.api.load_coordinator import LoadRun
        from worker.engine.load import Shard

        profile = LoadProfile(type="steady", duration_s=10, workers=2,
                              target_tps=2000)
        run = LoadRun("run-s", profile)
        run._shard_ctx = {"env": {"adapters": {}}, "scenario": {"id": "sc"},
                          "scenarios": [{"id": "sc"}], "weights": [1.0],
                          "heartbeat_action": None}
        coord.runs["run-s"] = run

        # both original shards already claimed by the 2 live workers
        await bus.push_shards("run-s", [
            {**profile.shard(i).to_dict(), **run._shard_ctx} for i in range(2)
        ])
        old0 = Shard.from_dict(await bus.claim_shard("run-s", timeout_s=0.1))
        await bus.claim_shard("run-s", timeout_s=0.1)
        assert await bus.pending_shards("run-s") == 0

        await coord.scale("run-s", 4)

        # 2 new shards queued for the 2 new workers, sliced as part of a 4-fleet
        assert await bus.pending_shards("run-s") == 2
        new = Shard.from_dict(await bus.claim_shard("run-s", timeout_s=0.1))
        assert new.worker_count == 4
        assert new.target_tps == 500
        # existing workers observe the retune and re-slice 1-of-2 -> 1-of-4
        params = await bus.get_retune("run-s")
        assert params["workers"] == 4 and params["target_tps"] == 2000
        assert old0.target_tps == 1000  # pre-retune slice
        old0.apply_retune(params)
        assert old0.worker_count == 4
        assert old0.target_tps == 500   # 2x1000 + 2x500 would have overdriven

    asyncio.run(go())


def test_scale_down_retunes_without_new_shards():
    prov = _RecordingProvisioner()
    bus = InMemoryLoadBus()
    coord = LoadCoordinator(bus, backbone=None, provisioner=prov)

    async def go():
        from orchestrator.api.load_coordinator import LoadRun

        profile = LoadProfile(type="steady", duration_s=10, workers=4,
                              target_tps=2000)
        run = LoadRun("run-d", profile)
        run._shard_ctx = {"env": {}, "scenario": {}, "scenarios": [{}],
                          "weights": [1.0], "heartbeat_action": None}
        coord.runs["run-d"] = run

        await coord.scale("run-d", 2)
        assert await bus.pending_shards("run-d") == 0  # nothing new queued
        params = await bus.get_retune("run-d")
        assert params["workers"] == 2  # survivors re-slice to halves

    asyncio.run(go())
