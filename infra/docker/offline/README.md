# Offline image builds (air-gapped hosts)

These Dockerfiles build the **minimal** PayProbe stack (portal, scenario-service,
orchestrator, auth-service, mcp-server — Redis is a stock image) on a host with **no
registry, PyPI or npm access**. Everything they need is staged into one build
context by `scripts/offline_build_kit.sh` on a machine that *does* have
internet: base-image tarballs (skopeo), Python wheels (`pip download`), the
prebuilt Angular portal (`ng build`), and — optionally, for adapters that open
Oracle sessions — the Oracle Instant Client libraries.

Differences from the online Dockerfiles, on purpose:

- No `# syntax=docker/dockerfile:1` directive: BuildKit would try to pull the
  frontend image from the registry. The built-in frontend is enough here.

- `pip install --no-index --find-links=/wheels`; no `apt-get upgrade` (no apt
  mirror). Refresh the base image tarball to pick up OS fixes.
- The orchestrator image installs `python-oracledb` and the Instant Client
  (`/opt/oracle`, `LD_LIBRARY_PATH`) so the in-process worker can open thick
  Oracle sessions. It does **not** install the Docker CLI or esbuild: dynamic
  load-worker provisioning (`PAYPROBE_WORKER_PROVISIONER=none`) and
  TypeScript code steps are unavailable; Python code steps work.
- The portal image copies a `dist/` built outside the container.

See `docs/operations/offline-minimal-deployment.md`.
