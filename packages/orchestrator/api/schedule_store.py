"""Scheduled regression runs — durable, file-backed schedule registry.

A *schedule* re-launches a saved run request (a project/set/scenario list against
an environment) on a cadence — nightly, hourly, every N seconds — so regression
suites run unattended and feed the run-history trend.

Cadence is intentionally dependency-free: ``interval_sec`` (every N seconds) or
``daily_at`` ("HH:MM" UTC). The background loop in the orchestrator calls
``due()`` periodically and triggers each due schedule, then ``mark_ran`` advances
its ``next_run_at``.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def next_after(sched: dict, after: datetime) -> datetime:
    """Compute the next fire time strictly after ``after``."""
    interval = sched.get("interval_sec")
    if interval:
        return after + timedelta(seconds=int(interval))
    daily_at = sched.get("daily_at")
    if daily_at:
        hh, mm = (int(x) for x in str(daily_at).split(":"))
        cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= after:
            cand += timedelta(days=1)
        return cand
    return after + timedelta(hours=1)  # sane fallback


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or uuid.uuid4().hex[:8]


class ScheduleStore:
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
                self._items = dict(json.loads(self._path.read_text()).get("schedules") or {})
            except (json.JSONDecodeError, OSError):
                self._items = {}

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"schedules": self._items}, indent=2))
        tmp.replace(self._path)

    # -- read ----------------------------------------------------------------

    def list(self) -> list[dict]:
        return [dict(v, id=k) for k, v in sorted(self._items.items())]

    def get(self, sid: str) -> dict | None:
        v = self._items.get(sid)
        return dict(v, id=sid) if v is not None else None

    def due(self, now: datetime | None = None) -> list[dict]:
        now = now or _now()
        out = []
        for sid, v in self._items.items():
            if not v.get("enabled", True):
                continue
            nxt = v.get("next_run_at")
            if nxt and datetime.fromisoformat(nxt) <= now:
                out.append(dict(v, id=sid))
        return out

    # -- write ---------------------------------------------------------------

    def create(self, draft: dict) -> dict:
        sid = _slug(draft.get("label", ""))
        if sid in self._items:
            sid = f"{sid}-{uuid.uuid4().hex[:4]}"
        now = _now()
        doc = {
            "label": draft.get("label", sid),
            #: "run" launches a functional regression run (default); "load"
            #: launches a load run from a LoadRunRequest body — an interval
            #: + soak profile gives a cyclic soak (loop soak → stop → repeat).
            "kind": draft.get("kind", "run"),
            "request": draft.get("request", {}),
            "interval_sec": draft.get("interval_sec"),
            "daily_at": draft.get("daily_at"),
            "enabled": draft.get("enabled", True),
            "created_at": now.isoformat(),
            "last_run_at": None,
            "next_run_at": next_after(draft, now).isoformat(),
        }
        with self._lock:
            self._items[sid] = doc
            self._save()
        return dict(doc, id=sid)

    def mark_ran(self, sid: str, when: datetime | None = None) -> None:
        when = when or _now()
        with self._lock:
            v = self._items.get(sid)
            if not v:
                return
            v["last_run_at"] = when.isoformat()
            v["next_run_at"] = next_after(v, when).isoformat()
            self._save()

    def set_enabled(self, sid: str, enabled: bool) -> dict | None:
        with self._lock:
            v = self._items.get(sid)
            if not v:
                return None
            v["enabled"] = enabled
            self._save()
        return self.get(sid)

    def delete(self, sid: str) -> None:
        with self._lock:
            if self._items.pop(sid, None) is None:
                raise KeyError(sid)
            self._save()
