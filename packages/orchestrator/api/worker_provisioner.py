"""Worker provisioning — give the portal a way to *spawn* load workers, not just
observe and drain them.

The fleet has always been manageable the **custom** way: an operator starts
``python -m worker.load_worker`` processes by hand (or from their own
orchestrator / k8s) and they claim shards off the shared Redis bus. That path is
unchanged and always available.

This module adds the **dynamic** way: a pluggable provisioner the orchestrator
drives so a load run can bring its own fleet up (and tear it down) on launch, and
so the portal can scale a run's workers live. A provisioner is a thin adapter
over *whatever actually runs a worker process*:

* :class:`LocalSubprocessProvisioner` — spawn ``python -m worker.load_worker``
  subprocesses on the orchestrator host. Zero infra; great for dev and a single
  box. (Still needs ``REDIS_URL`` so the workers and orchestrator share a bus.)
* :class:`DockerProvisioner` — start each worker as a fresh container via
  ``docker run -d`` against the prebuilt worker image. Self-contained: needs only
  the docker socket + image name, no compose file.
* :class:`ComposeProvisioner` — launch worker containers via ``docker compose
  run -d`` against a ``load-worker`` service; reuses the service's env/network
  but needs the compose file mounted into the orchestrator.
* :class:`NullProvisioner` — no auto-spawn. ``available`` is ``False`` and
  :meth:`scale` is a no-op carrying the manual command, so the portal cleanly
  falls back to the custom path.

A worker process is bound to one run (``LOAD_RUN_ID``) and claims exactly one
shard, so "scale run R to N" means "ensure N worker processes exist for run R".
Containers/processes are named/tracked per run so several runs can coexist and
each can be scaled or reaped independently.

Select the backend with ``PAYPROBE_WORKER_PROVISIONER`` = ``auto`` (default) |
``local`` | ``compose`` | ``none``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger("payprobe.worker_provisioner")


def manual_command(run_id: str, redis_url: str | None) -> str:
    """The copy-pasteable CLI an operator uses for the custom (manual) path."""
    redis = redis_url or os.environ.get("REDIS_URL") or "redis://localhost:6379"
    return f"REDIS_URL={redis} LOAD_RUN_ID={run_id} python -m worker.load_worker"


class WorkerProvisioner(ABC):
    """Brings worker processes up/down for a run. Implementations must be safe to
    call repeatedly — :meth:`scale` is declarative ("make it N"), not additive."""

    #: short backend id surfaced to the portal ("local" | "compose" | "none")
    kind: str = "none"

    #: True when the backend can actually start workers in this deployment.
    available: bool = False

    #: why it's unavailable (shown in the portal so the operator knows what to fix)
    detail: str = ""

    @abstractmethod
    async def scale(self, run_id: str, desired: int) -> dict[str, Any]:
        """Ensure exactly ``desired`` workers exist for ``run_id``. Returns a
        status dict: ``{kind, requested, running, ...}``."""

    @abstractmethod
    async def count(self, run_id: str) -> int:
        """How many workers this provisioner currently manages for ``run_id``."""

    async def teardown(self, run_id: str) -> None:
        """Reap every worker for a finished run. Default: scale to zero."""
        try:
            await self.scale(run_id, 0)
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            log.debug("teardown of run %s failed: %s", run_id, exc)

    def capabilities(self) -> dict[str, Any]:
        """Describe the backend for the portal's fleet panel."""
        return {"kind": self.kind, "available": self.available, "detail": self.detail}

    # -- shared helpers ------------------------------------------------------
    @staticmethod
    def _worker_env(run_id: str, redis_url: str | None) -> dict[str, str]:
        env = dict(os.environ)
        env["LOAD_RUN_ID"] = run_id
        if redis_url:
            env["REDIS_URL"] = redis_url
        return env


class NullProvisioner(WorkerProvisioner):
    """No automatic provisioning — the operator brings workers up themselves."""

    kind = "none"
    available = False

    def __init__(self, detail: str = "auto-provisioning disabled; start workers manually") -> None:
        self.detail = detail

    async def scale(self, run_id: str, desired: int) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "available": False,
            "requested": desired,
            "running": 0,
            "notice": f"No worker provisioner configured. Start workers manually: "
                      f"{manual_command(run_id, None)}",
        }

    async def count(self, run_id: str) -> int:
        return 0

    async def teardown(self, run_id: str) -> None:  # nothing to reap
        return None


class LocalSubprocessProvisioner(WorkerProvisioner):
    """Spawn ``python -m worker.load_worker`` subprocesses on this host.

    Needs ``REDIS_URL`` so the spawned workers and the orchestrator share a bus
    (with the in-memory bus there is nothing for a separate process to join — the
    coordinator's in-process fallback covers that case instead)."""

    kind = "local"

    def __init__(self, redis_url: str | None = None, *, python: str | None = None) -> None:
        self.redis_url = redis_url or os.environ.get("REDIS_URL")
        self.python = python or sys.executable or "python"
        self.available = bool(self.redis_url)
        self.detail = "" if self.available else "REDIS_URL is required for external workers"
        #: run_id -> live subprocess handles
        self._procs: dict[str, list[asyncio.subprocess.Process]] = {}
        self._lock = asyncio.Lock()

    def _reap(self, run_id: str) -> list[asyncio.subprocess.Process]:
        """Drop handles for workers that have already exited; return the live set."""
        live = [p for p in self._procs.get(run_id, []) if p.returncode is None]
        self._procs[run_id] = live
        return live

    async def _spawn_one(self, run_id: str) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            self.python, "-m", "worker.load_worker",
            env=self._worker_env(run_id, self.redis_url),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def scale(self, run_id: str, desired: int) -> dict[str, Any]:
        if not self.available:
            return {"kind": self.kind, "available": False, "requested": desired,
                    "running": 0, "notice": self.detail}
        desired = max(0, int(desired))
        async with self._lock:
            live = self._reap(run_id)
            # scale up
            while len(live) < desired:
                proc = await self._spawn_one(run_id)
                live.append(proc)
                log.info("run %s: spawned local worker pid=%s (%d/%d)",
                         run_id, proc.pid, len(live), desired)
            # scale down — terminate the most recently added first
            while len(live) > desired:
                proc = live.pop()
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                log.info("run %s: terminated local worker pid=%s (-> %d)",
                         run_id, proc.pid, len(live))
            self._procs[run_id] = live
            return {"kind": self.kind, "available": True, "requested": desired,
                    "running": len(live)}

    async def count(self, run_id: str) -> int:
        async with self._lock:
            return len(self._reap(run_id))

    async def teardown(self, run_id: str) -> None:
        await self.scale(run_id, 0)
        self._procs.pop(run_id, None)


class _DockerCliProvisioner(WorkerProvisioner):
    """Shared plumbing for backends that run each worker as its own container via
    the ``docker`` CLI. Containers are named ``<project>-lw-<run_id>-<i>`` so a
    run's fleet can be listed, scaled, and reaped by name prefix, and several
    runs coexist independently. Subclasses implement :meth:`_start_args` — the
    ``docker`` arguments that launch one worker container bound to a run."""

    def __init__(self, *, project: str | None = None, redis_url: str | None = None) -> None:
        self.project = project or os.environ.get("COMPOSE_PROJECT_NAME", "payprobe")
        self.redis_url = redis_url or os.environ.get("REDIS_URL")
        self.docker = shutil.which("docker")
        self._lock = asyncio.Lock()

    # subclasses fill these in
    def _start_args(self, run_id: str, name: str) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def _name_prefix(self, run_id: str) -> str:
        # docker names allow [a-zA-Z0-9_.-]; run ids are hex+dashes already.
        return f"{self.project}-lw-{run_id}"

    async def _run(self, *args: str) -> tuple[int, str, str]:
        env = dict(os.environ)
        if self.redis_url:
            env["REDIS_URL"] = self.redis_url
        proc = await asyncio.create_subprocess_exec(
            self.docker, *args, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode().strip(), err.decode().strip()

    async def count(self, run_id: str) -> int:
        rc, out, _err = await self._run(
            "ps", "-q", "--filter", f"name={self._name_prefix(run_id)}-")
        if rc != 0:
            return 0
        return len([line for line in out.splitlines() if line.strip()])

    async def _names(self, run_id: str) -> list[str]:
        # -a so we also see exited (e.g. --rm-pending) containers for accurate
        # scale-down accounting.
        rc, out, _err = await self._run(
            "ps", "-a", "--format", "{{.Names}}",
            "--filter", f"name={self._name_prefix(run_id)}-")
        if rc != 0:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def _running_count(self, run_id: str) -> int:
        """Containers actually *running* now (not merely created or exited)."""
        rc, out, _err = await self._run(
            "ps", "-q", "--filter", f"name={self._name_prefix(run_id)}-")
        return 0 if rc != 0 else len([ln for ln in out.splitlines() if ln.strip()])

    async def _exited_names(self, run_id: str) -> list[str]:
        rc, out, _err = await self._run(
            "ps", "-a", "--format", "{{.Names}}",
            "--filter", f"name={self._name_prefix(run_id)}-",
            "--filter", "status=exited")
        return [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []

    async def _prune_exited(self, run_id: str) -> None:
        """Reap exited worker containers so a prior failed attempt doesn't linger
        or skew the running count."""
        for name in await self._exited_names(run_id):
            await self._run("rm", "-f", name)

    async def _failed_logs(self, run_id: str) -> str:
        """Last log lines of a crashed worker — the real reason it died (e.g.
        'cannot connect to redis://redis:6379'), surfaced back to the operator."""
        names = await self._exited_names(run_id)
        if not names:
            return ""
        _rc, out, err = await self._run("logs", "--tail", "12", names[0])
        return (out or err or "").strip()[-600:]

    async def scale(self, run_id: str, desired: int) -> dict[str, Any]:
        if not self.available:
            return {"kind": self.kind, "available": False, "requested": desired,
                    "running": 0, "notice": self.detail}
        desired = max(0, int(desired))
        async with self._lock:
            # clear any dead containers from a prior attempt first, so counts and
            # the index don't drift.
            await self._prune_exited(run_id)
            names = await self._names(run_id)
            running = len(names)
            errors: list[str] = []
            # scale up: one new container per missing worker, named by index so a
            # re-scale doesn't collide with surviving containers.
            i = _next_index(names)
            while running < desired:
                name = f"{self._name_prefix(run_id)}-{i}"
                rc, _out, err = await self._run(*self._start_args(run_id, name))
                i += 1
                if rc != 0:
                    errors.append(err or f"failed to start {name}")
                    break
                running += 1
            # scale down: remove the highest-indexed containers first
            if running > desired:
                victims = sorted(
                    names, key=_trailing_int, reverse=True
                )[: running - desired]
                for name in victims:
                    rc, _out, err = await self._run("rm", "-f", name)
                    if rc == 0:
                        running -= 1
                    else:
                        errors.append(err or f"failed to remove {name}")
            # Report the count that's actually RUNNING — a `docker run` returning 0
            # only means the container was created; a worker that can't reach Redis
            # exits a beat later. Counting those as "running" is what made the panel
            # claim "N workers" while 0 ever joined.
            alive = await self._running_count(run_id)
            if alive < desired and not errors:
                why = await self._failed_logs(run_id)
                errors.append(
                    f"{desired - alive} worker(s) exited on startup"
                    + (f" — {why}" if why
                       else " — they couldn't stay up. Check the worker image and "
                            "that REDIS_URL / the worker network reach Redis.")
                )
            result: dict[str, Any] = {
                "kind": self.kind, "available": True,
                "requested": desired, "running": alive,
            }
            if errors:
                result["notice"] = "; ".join(errors)
            return result

    async def teardown(self, run_id: str) -> None:
        if not self.available:
            return
        for name in await self._names(run_id):
            await self._run("rm", "-f", name)


class DockerProvisioner(_DockerCliProvisioner):
    """Run each worker as a fresh container via ``docker run -d`` against the
    prebuilt worker image. Self-contained — needs only the docker socket and the
    image name (``PAYPROBE_WORKER_IMAGE``); no compose file. Attach the worker to
    the network where Redis is reachable with ``PAYPROBE_WORKER_NETWORK`` (e.g.
    ``payprobe_default``) so ``REDIS_URL=redis://redis:6379`` resolves."""

    kind = "docker"

    def __init__(
        self,
        *,
        image: str | None = None,
        network: str | None = None,
        project: str | None = None,
        redis_url: str | None = None,
    ) -> None:
        super().__init__(project=project, redis_url=redis_url)
        self.image = image or os.environ.get("PAYPROBE_WORKER_IMAGE", "")
        self.network = network or os.environ.get("PAYPROBE_WORKER_NETWORK", "")
        if not self.docker:
            self.available, self.detail = False, "docker CLI not found on the orchestrator"
        elif not self.image:
            self.available, self.detail = False, "PAYPROBE_WORKER_IMAGE is not set"
        else:
            self.available, self.detail = True, ""

    def _start_args(self, run_id: str, name: str) -> list[str]:
        # `docker run -d` (no --rm) => detached, and a crashed container stays as
        # "exited" long enough for the provisioner to read its logs and surface
        # why the worker died. Dead containers are reaped by ``_prune_exited`` /
        # ``teardown``. Override the image's batch entrypoint to the worker module.
        args = ["run", "-d", "--name", name]
        if self.network:
            args += ["--network", self.network]
        args += ["-e", f"LOAD_RUN_ID={run_id}"]
        if self.redis_url:
            args += ["-e", f"REDIS_URL={self.redis_url}"]
        args += ["--entrypoint", "python", self.image, "-m", "worker.load_worker"]
        return args


class ComposeProvisioner(_DockerCliProvisioner):
    """Run worker containers via ``docker compose run -d`` against a service
    (default ``load-worker``). Reuses the service's env/network from the compose
    file automatically, at the cost of needing that file mounted into the
    orchestrator (``PAYPROBE_COMPOSE_FILE``)."""

    kind = "compose"

    def __init__(
        self,
        *,
        compose_file: str | None = None,
        service: str | None = None,
        project: str | None = None,
        redis_url: str | None = None,
    ) -> None:
        super().__init__(project=project, redis_url=redis_url)
        self.compose_file = compose_file or os.environ.get("PAYPROBE_COMPOSE_FILE", "")
        self.service = service or os.environ.get("PAYPROBE_LOAD_WORKER_SERVICE", "load-worker")
        if not self.docker:
            self.available, self.detail = False, "docker CLI not found on the orchestrator"
        elif not self.compose_file:
            self.available, self.detail = False, "PAYPROBE_COMPOSE_FILE is not set"
        else:
            self.available, self.detail = True, ""

    def _start_args(self, run_id: str, name: str) -> list[str]:
        return ["compose", "-p", self.project, "-f", self.compose_file,
                "run", "-d", "--name", name,
                "-e", f"LOAD_RUN_ID={run_id}", self.service]


def _next_index(names: list[str]) -> int:
    """Lowest container index not already in use (so re-scale never collides)."""
    used = {_trailing_int(n) for n in names}
    i = 0
    while i in used:
        i += 1
    return i


def _trailing_int(name: str) -> int:
    tail = name.rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def make_provisioner(
    kind: str | None = None, *, redis_url: str | None = None
) -> WorkerProvisioner:
    """Build the provisioner selected by ``PAYPROBE_WORKER_PROVISIONER``.

    Explicit ``docker`` / ``compose`` / ``local`` / ``none`` override the
    heuristic. ``auto`` (default) prefers the most self-contained backend that's
    actually usable: a fresh container per worker via ``docker run`` when
    ``PAYPROBE_WORKER_IMAGE`` is set, else the Compose backend when a compose
    file is configured, else local subprocesses when ``REDIS_URL`` is set, else
    none."""
    kind = (kind or os.environ.get("PAYPROBE_WORKER_PROVISIONER", "auto")).lower()
    redis_url = redis_url or os.environ.get("REDIS_URL")

    if kind == "none":
        return NullProvisioner()
    if kind == "local":
        return LocalSubprocessProvisioner(redis_url)
    if kind == "docker":
        return DockerProvisioner(redis_url=redis_url)
    if kind == "compose":
        return ComposeProvisioner(redis_url=redis_url)

    # auto
    if not redis_url:
        return NullProvisioner("REDIS_URL not set; the in-process fallback handles "
                               "single-node runs, external workers need Redis")
    docker = DockerProvisioner(redis_url=redis_url)
    if docker.available:
        return docker
    compose = ComposeProvisioner(redis_url=redis_url)
    if compose.available:
        return compose
    return LocalSubprocessProvisioner(redis_url)
