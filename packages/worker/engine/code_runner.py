"""Sandboxed execution of ``code`` nodes.

A ``code`` node runs a user-supplied Python or TypeScript/JavaScript snippet in
a fresh subprocess. The snippet receives the run ``context`` — the same
``${step.response.*}`` data available to every other node — and returns an
object that is exposed downstream as ``${node_id.response.*}``.

Isolation model (see the editor's "Sandbox" choice):
- each invocation is a *new* subprocess with a wall-clock timeout;
- on POSIX a soft CPU rlimit (and, for Python, an address-space rlimit) is set
  via ``preexec_fn`` so a runaway snippet is killed rather than starving the
  worker; file-size and open-file rlimits cap disk and fd abuse;
- the child runs in a throwaway working directory and inherits a trimmed
  environment with no scenario state beyond the JSON payload on its stdin;
- on Linux the child is placed in a **fresh network namespace** (via
  ``unshare``) so the snippet has no network egress — this is the boundary that
  turns "runs untrusted code" from SSRF/exfiltration into a closed box.

Network isolation is controlled by ``PAYPROBE_CODE_SANDBOX``:
- ``auto`` (default) — isolate when the kernel allows it, run without it
  otherwise (so dev boxes and CI without user namespaces still work);
- ``strict`` — require the network sandbox; refuse to run code if it can't be
  established (use this in any environment that accepts untrusted authors);
- ``off`` — disable the namespace (trusted-author deployments only).

A network namespace blocks egress but does **not** isolate the filesystem for
reads; for fully untrusted multi-tenant code, run the worker under nsjail /
gVisor / a microVM in addition to this layer. The orchestrator additionally
refuses ``code`` execution over ``/nodes/execute`` when auth is disabled unless
a sandbox is active (see ``api/main.py``).

The contract the snippet sees:
- ``context`` (alias ``inputs``): a dict of ``{step_id: {request, response}}``
  plus ``{"loop": {...}}`` inside loop bodies;
- it returns the result with ``return`` (Python: the snippet is wrapped in a
  function body; JS/TS: wrapped in an async arrow). A non-dict return is
  wrapped as ``{"value": <result>}``. ``print`` / ``console.log`` are routed to
  stderr so stdout stays a single JSON line.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_MS = 5_000
MAX_TIMEOUT_MS = 60_000
CPU_SECONDS = 15
PY_MEM_LIMIT_BYTES = 256 * 1024 * 1024
FSIZE_LIMIT_BYTES = 16 * 1024 * 1024  # max bytes a snippet may write to disk
NOFILE_LIMIT = 128  # max open file descriptors


class SandboxUnavailable(RuntimeError):
    """Raised when PAYPROBE_CODE_SANDBOX=strict but no network sandbox exists."""


def _sandbox_mode() -> str:
    return os.environ.get("PAYPROBE_CODE_SANDBOX", "auto").strip().lower()


# Probe result is cached per mode so we pay the spawn cost once. Keyed by mode
# so tests can flip the env and call _reset_sandbox_cache().
_SANDBOX_PREFIX_CACHE: dict[str, list[str] | None] = {}


def _probe_network_sandbox() -> list[str] | None:
    """Find an ``unshare`` invocation that creates a network namespace here.

    Prefers the rootless form (new user namespace → full caps inside → new net
    namespace) and falls back to the privileged form (works when the process
    already has CAP_SYS_ADMIN, e.g. a root container). Returns the argv prefix
    to prepend to the child command, or None if neither works.
    """
    if os.name != "posix":
        return None
    unshare = shutil.which("unshare")
    if not unshare:
        return None
    candidates = [
        [unshare, "--user", "--map-root-user", "--net"],
        [unshare, "--net"],
    ]
    for prefix in candidates:
        try:
            res = subprocess.run(
                [*prefix, "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:  # noqa: BLE001 - unshare missing flags / blocked
            continue
        if res.returncode == 0:
            return prefix
    return None


def _network_sandbox_prefix() -> list[str] | None:
    """The cached argv prefix that network-isolates a child (or None)."""
    mode = _sandbox_mode()
    if mode == "off":
        return None
    if mode not in _SANDBOX_PREFIX_CACHE:
        _SANDBOX_PREFIX_CACHE[mode] = _probe_network_sandbox()
    return _SANDBOX_PREFIX_CACHE[mode]


def _reset_sandbox_cache() -> None:  # pragma: no cover - test helper
    _SANDBOX_PREFIX_CACHE.clear()


def network_isolation_active() -> bool:
    """True when code nodes currently run without network access."""
    return _network_sandbox_prefix() is not None


@dataclass
class CodeResult:
    ok: bool
    output: dict = field(default_factory=dict)
    error: str | None = None
    logs: str = ""


# -- child bootstraps --------------------------------------------------------

# Reads {"context", "code"} from stdin, wraps the snippet in a function body so
# ``return`` works, redirects the snippet's stdout to stderr, and writes a
# single JSON result line to the real stdout.
_PY_BOOTSTRAP = r"""
import sys, json
def _run(data):
    ctx = data.get("context", {})
    src = data.get("code", "")
    body = "def __main(context, inputs):\n" + "".join(
        "    " + line + "\n" for line in src.split("\n")
    )
    glb = {}
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        exec(compile(body, "<code-node>", "exec"), glb)
        result = glb["__main"](ctx, data.get("inputs") or {})
    finally:
        sys.stdout = real_stdout
    return {} if result is None else result
try:
    data = json.loads(sys.stdin.read())
    out = _run(data)
    if not isinstance(out, dict):
        out = {"value": out}
    sys.stdout.write(json.dumps({"ok": True, "output": out}))
except Exception as exc:
    sys.stdout.write(json.dumps({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}))
"""

# Node bootstrap. TypeScript is transpiled with esbuild when available; without
# it, JS-compatible snippets still run (TS-only syntax then errors clearly).
_NODE_BOOTSTRAP = r"""
const fs = require('fs');
console.log = console.error;  // keep stdout a single JSON line
(async () => {
  let data;
  try { data = JSON.parse(fs.readFileSync(0, 'utf8')); }
  catch (e) { process.stdout.write(JSON.stringify({ ok: false, error: 'bad input payload' })); return; }
  let src = '(async (context, inputs) => {\n' + (data.code || '') + '\n})';
  if (data.language === 'typescript') {
    try { src = require('esbuild').transformSync(src, { loader: 'ts' }).code; }
    catch (e) { /* esbuild unavailable — fall back to evaluating as JS */ }
  }
  try {
    const fn = (0, eval)(src);
    let out = await fn(data.context || {}, data.inputs || {});
    if (out === undefined || out === null) out = {};
    if (typeof out !== 'object' || Array.isArray(out)) out = { value: out };
    process.stdout.write(JSON.stringify({ ok: true, output: out }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.stack) || e) }));
  }
})();
"""


def _make_limits(mem_bytes: int | None):
    """Build a ``preexec_fn`` applying soft rlimits in the child (POSIX only)."""

    def _apply() -> None:  # pragma: no cover - runs in the forked child
        import resource

        try:
            resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))
        except (ValueError, OSError):
            pass
        # Cap bytes written to disk and the number of open fds so a snippet
        # can't fill the volume or exhaust descriptors even where it has reads.
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_LIMIT_BYTES, FSIZE_LIMIT_BYTES))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (NOFILE_LIMIT, NOFILE_LIMIT))
        except (ValueError, OSError):
            pass
        if mem_bytes is not None:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
            except (ValueError, OSError):
                pass

    return _apply


def _child_env() -> dict:
    """A trimmed environment: enough to locate runtimes and global node modules."""
    env = {"PATH": os.environ.get("PATH", "")}
    for key in ("NODE_PATH", "LANG", "LC_ALL", "SYSTEMROOT"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


async def run_code(
    language: str,
    code: str,
    context: dict,
    timeout_ms: int | None = None,
    inputs: dict | None = None,
) -> CodeResult:
    """Execute ``code`` in ``language`` and return its result.

    ``context`` is the full run context (``{step_id: {request, response}}``);
    ``inputs`` is the node's resolved input map (``${...}`` references already
    resolved), exposed to the snippet as ``inputs`` for clean chaining.
    """
    try:
        timeout = min(max(int(timeout_ms or DEFAULT_TIMEOUT_MS), 1), MAX_TIMEOUT_MS) / 1000
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_MS / 1000

    # Reserved ``__``-prefixed context keys (e.g. ``__gen__``, the live generator
    # object) are engine internals, not snippet data, and aren't JSON-serialisable.
    safe_context = {k: v for k, v in context.items() if not str(k).startswith("__")}
    payload = json.dumps(
        {"context": safe_context, "inputs": inputs or {}, "code": code, "language": language},
        default=str,
    ).encode()

    if language == "python":
        argv = [sys.executable, "-I", "-c", _PY_BOOTSTRAP]
        mem = PY_MEM_LIMIT_BYTES
    elif language in ("typescript", "javascript"):
        node = shutil.which("node")
        if not node:
            return CodeResult(
                False, {}, "Node.js runtime is not available for TypeScript code nodes"
            )
        argv = [node, "-e", _NODE_BOOTSTRAP]
        mem = None  # Node needs a large virtual address space; cap CPU only
    else:
        return CodeResult(False, {}, f"unsupported code language '{language}'")

    # Network isolation. In strict mode, refuse to run rather than execute a
    # snippet with egress; in auto mode, run with isolation when available.
    sandbox = _network_sandbox_prefix()
    if sandbox is None and _sandbox_mode() == "strict":
        return CodeResult(
            False,
            {},
            "code sandbox required (PAYPROBE_CODE_SANDBOX=strict) but a network "
            "namespace could not be created on this host",
        )
    if sandbox:
        argv = [*sandbox, *argv]

    preexec = _make_limits(mem) if os.name == "posix" else None
    # Throwaway cwd so relative writes land somewhere disposable, not next to
    # the worker. Cleaned up after the child exits.
    workdir = tempfile.mkdtemp(prefix="pp-code-")
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=preexec,
                env=_child_env(),
                cwd=workdir,
            )
        except Exception as exc:  # runtime missing / spawn failure
            return CodeResult(False, {}, f"failed to start {language} runtime: {exc}")

        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(payload), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CodeResult(False, {}, f"code timed out after {int(timeout * 1000)} ms")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    logs = err_b.decode(errors="replace").strip()
    text = out_b.decode(errors="replace").strip()
    if not text:
        return CodeResult(False, {}, logs or "code produced no output", logs)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return CodeResult(False, {}, f"could not parse code output: {text[:300]}", logs)

    if data.get("ok"):
        output = data.get("output") or {}
        if not isinstance(output, dict):
            output = {"value": output}
        return CodeResult(True, output, None, logs)
    return CodeResult(False, {}, data.get("error") or "code failed", logs)
