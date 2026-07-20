"""Sign-off snapshots — immutable Go/No-Go artifacts + approval trail.

A *sign-off* is the frozen fact that gives (or withholds) the green light to run
on production. Unlike a run report — a live view that changes as data changes —
a sign-off is an **immutable snapshot**: the run summary, the gate verdict, the
provenance stamp, and a tamper-evident content hash, captured at the moment of
certification (see ADR-0003). Evidence is never mutated after creation; only the
**approval trail** (who reviewed / signed off, when) appends.

The store also keeps a **baseline pointer** per ``(project, pack, environment)``
recording the last run that achieved a GO verdict — the only meaningful baseline
for the regression gate ("zero regressions vs what actually shipped").

Dependency-free and thread-safe, mirroring :class:`~.simulator_store.SimulatorStore`
(a tiny JSON file, or ``:memory:`` for dev/test). An optional ``box`` (duck-typed
``encrypt_doc`` / ``decrypt_doc``, e.g. a ``SecretBox``) encrypts secret-named
fields at rest; with no box it is a passthrough, same as before.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _baseline_key(project: str | None, pack: str | None, environment: str | None) -> str:
    """Stable composite key — a GO is only valid for its env + pack + project."""
    return f"{project or '-'}::{pack or '-'}::{environment or '-'}"


class _Passthrough:
    """No-op box used when encryption-at-rest isn't configured."""

    def encrypt_doc(self, doc: Any) -> Any:  # noqa: D401 - trivial
        return doc

    def decrypt_doc(self, doc: Any) -> Any:
        return doc


class SignoffStore:
    def __init__(self, path: str | None = None, box: Any | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._box = box or _Passthrough()
        self._items: dict[str, dict] = {}
        self._baselines: dict[str, str] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                data = json.loads(self._path.read_text())
                self._items = {
                    k: self._box.decrypt_doc(v)
                    for k, v in (data.get("signoffs") or {}).items()
                }
                self._baselines = dict(data.get("baselines") or {})
            except (json.JSONDecodeError, OSError):
                self._items, self._baselines = {}, {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "signoffs": {k: self._box.encrypt_doc(v) for k, v in self._items.items()},
            "baselines": self._baselines,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)

    # -- write ---------------------------------------------------------------

    def create(self, snapshot: dict) -> dict:
        """Freeze a sign-off snapshot. The evidence is immutable from here on.

        ``snapshot`` must already carry ``run_id``, ``verdict``, ``gate_result``,
        ``provenance``, ``content_hash`` and ``summary``. A GO verdict advances
        the baseline pointer for its ``(project, pack, environment)``.
        """
        sid = uuid.uuid4().hex[:12]
        now = _now_iso()
        doc = {
            **snapshot,
            "id": sid,
            "created_at": now,
            "frozen": True,
            "approvals": [],
        }
        with self._lock:
            self._items[sid] = doc
            if snapshot.get("verdict") == "GO":
                key = _baseline_key(
                    snapshot.get("project"), snapshot.get("pack"),
                    snapshot.get("environment"),
                )
                self._baselines[key] = snapshot.get("run_id")
            self._save()
        return doc

    def add_approval(
        self, sid: str, *, by: str, decision: str = "approved", note: str = "",
    ) -> dict | None:
        """Append to the approval trail. Does not touch the frozen evidence, so
        the ``content_hash`` (computed over evidence only) stays valid."""
        with self._lock:
            doc = self._items.get(sid)
            if doc is None:
                return None
            doc["approvals"].append({
                "by": by, "decision": decision, "note": note, "at": _now_iso(),
            })
            self._save()
            return doc

    # -- read ----------------------------------------------------------------

    def get(self, sid: str) -> dict | None:
        return self._items.get(sid)

    def list(
        self, *, run_id: str | None = None, project: str | None = None,
        verdict: str | None = None,
    ) -> list[dict]:
        items = sorted(
            self._items.values(), key=lambda d: d.get("created_at", ""), reverse=True,
        )
        if run_id is not None:
            items = [d for d in items if d.get("run_id") == run_id]
        if project is not None:
            items = [d for d in items if d.get("project") == project]
        if verdict is not None:
            items = [d for d in items if d.get("verdict") == verdict]
        return items

    def baseline_run(
        self, *, project: str | None, pack: str | None, environment: str | None,
    ) -> str | None:
        """The last GO run id for this (project, pack, environment), if any."""
        return self._baselines.get(_baseline_key(project, pack, environment))
