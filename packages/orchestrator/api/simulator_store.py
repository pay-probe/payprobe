"""Saved host/responder simulators — durable, file-backed registry.

A *saved simulator* is a named :class:`~worker.adapters.tcp.responder.TcpResponder`
config you can start, stop, clear and re-use, rather than re-pasting JSON each
time. The orchestrator keeps the *running* responders separately (in-memory, in
``main.SIMULATORS``); this store only holds the definitions plus an ``enabled``
flag — enabled simulators are auto-started on orchestrator boot.

Mirrors :class:`~.schedule_store.ScheduleStore`: a tiny, dependency-free,
thread-safe JSON file (or ``:memory:`` for dev/test). Running ids are the saved
ids, so a config and its live responder share one identity throughout the portal.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


def _has_chaos(config: dict) -> bool:
    """Does this responder config inject faults (global or per-rule)?"""
    if config.get("chaos"):
        return True
    return any("chaos" in (r.get("respond") or {}) for r in config.get("rules", []))


class SimulatorStore:
    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = (
            Path(path) if path and path not in (":memory:", "") else None
        )
        self._items: dict[str, dict] = {}
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path and self._path.is_file():
            try:
                self._items = dict(
                    json.loads(self._path.read_text()).get("simulators") or {}
                )
            except (json.JSONDecodeError, OSError):
                self._items = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"simulators": self._items}, indent=2))
        tmp.replace(self._path)

    # -- shaping -------------------------------------------------------------

    def _info(self, sid: str, v: dict) -> dict:
        cfg = v.get("config") or {}
        return {
            "id": sid,
            "label": v.get("label", sid),
            "config": cfg,
            "enabled": bool(v.get("enabled", False)),
            "protocol": cfg.get("protocol", "iso8583"),
            "rules": len(cfg.get("rules", [])),
            "chaos": _has_chaos(cfg),
            "proxy": str(cfg.get("kind", "")).lower() == "proxy",
            "created_at": v.get("created_at"),
            "updated_at": v.get("updated_at"),
        }

    # -- read ----------------------------------------------------------------

    def list(self) -> list[dict]:
        return [self._info(k, v) for k, v in sorted(self._items.items())]

    def get(self, sid: str) -> dict | None:
        v = self._items.get(sid)
        return self._info(sid, v) if v is not None else None

    def has(self, sid: str) -> bool:
        return sid in self._items

    def enabled(self) -> list[dict]:
        return [self._info(k, v) for k, v in self._items.items() if v.get("enabled")]

    # -- write ---------------------------------------------------------------

    def create(self, draft: dict) -> dict:
        sid = _slug(draft.get("label", ""))
        if sid in self._items:
            sid = f"{sid}-{uuid.uuid4().hex[:4]}"
        now = _now_iso()
        doc = {
            "label": draft.get("label", sid),
            "config": draft.get("config") or {},
            "enabled": bool(draft.get("enabled", False)),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._items[sid] = doc
            self._save()
        return self._info(sid, doc)

    def update(self, sid: str, patch: dict) -> dict | None:
        with self._lock:
            v = self._items.get(sid)
            if not v:
                return None
            if patch.get("label") is not None:
                v["label"] = patch["label"]
            if patch.get("config") is not None:
                v["config"] = patch["config"]
            if patch.get("enabled") is not None:
                v["enabled"] = bool(patch["enabled"])
            v["updated_at"] = _now_iso()
            self._save()
        return self.get(sid)

    def set_enabled(self, sid: str, enabled: bool) -> dict | None:
        return self.update(sid, {"enabled": enabled})

    def delete(self, sid: str) -> None:
        with self._lock:
            if self._items.pop(sid, None) is None:
                raise KeyError(sid)
            self._save()
