"""Durable run registry.

Persists every run so it can be reviewed after it finishes (and after the
service restarts). The worker engine's run summary already contains the full
per-scenario / per-step detail (request, response, assertions, duration,
status), so we store that document verbatim plus a few top-level columns for
cheap listing.

SQLite is used for the same reason as the scenario-service: zero-config, ample
for the ~30-user portal. The store is synchronous (no awaits between read and
write), which is fine under asyncio for this workload.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    environment    TEXT,
    label          TEXT,
    project_id     TEXT,
    scenario_count INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    completed_at   TEXT,
    summary        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _counts(summary: dict | None) -> dict:
    """Roll up per-scenario outcomes for the registry list."""
    scenarios = (summary or {}).get("scenarios", []) or []
    out = {"total": len(scenarios), "passed": 0, "failed": 0, "blocked": 0}
    for s in scenarios:
        st = s.get("status")
        if st in out:
            out[st] += 1
    return out


class RunStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self.durable = db_path != ":memory:"
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets multiple orchestrator replicas sharing the DB file read
        # concurrently while one writes (no-op for the :memory: dev store).
        if db_path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
            except sqlite3.OperationalError:
                pass
        self._conn.executescript(_SCHEMA)
        # defensive migration for DBs created before `label` existed
        try:
            self._conn.execute("ALTER TABLE runs ADD COLUMN label TEXT")
        except sqlite3.OperationalError:
            pass
        # defensive migration for DBs created before `project_id` existed
        try:
            self._conn.execute("ALTER TABLE runs ADD COLUMN project_id TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def create(
        self,
        run_id: str,
        environment: str,
        scenario_count: int,
        label: str = "",
        project_id: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO runs (id, status, environment, label, project_id, "
            "scenario_count, created_at) VALUES (?, 'pending', ?, ?, ?, ?, ?)",
            (run_id, environment, label, project_id or None, scenario_count, _now()),
        )
        self._conn.commit()

    def mark_running(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE runs SET status='running', started_at=? WHERE id=?",
            (_now(), run_id),
        )
        self._conn.commit()

    def finish(self, run_id: str, status: str, summary: dict) -> None:
        self._conn.execute(
            "UPDATE runs SET status=?, summary=?, completed_at=? WHERE id=?",
            (status, json.dumps(summary), _now(), run_id),
        )
        self._conn.commit()

    def unfinished(self, label_prefix: str | None = None) -> list[dict]:
        """Rows still in a non-terminal state (``pending``/``running``).

        These are candidates for reconciliation: after a crash or restart the
        in-memory coordinator that would have called :meth:`finish` is gone, so
        the DB row is stranded as ``running`` forever. ``label_prefix`` scopes
        the query (e.g. ``"load:"`` for load runs only)."""
        q = "SELECT id, status, label, started_at, created_at FROM runs WHERE status IN ('pending','running')"
        params: list[Any] = []
        if label_prefix:
            q += " AND label LIKE ?"
            params.append(label_prefix + "%")
        rows = self._conn.execute(q, params).fetchall()
        return [
            {"id": r["id"], "status": r["status"], "label": r["label"],
             "started_at": r["started_at"], "created_at": r["created_at"]}
            for r in rows
        ]

    def reconcile_orphans(
        self,
        live_ids: set[str],
        *,
        label_prefix: str | None = None,
        status: str = "interrupted",
        reason: str = "orphaned: no live run owner (service restart or crash)",
    ) -> list[str]:
        """Close out unfinished rows that no live run owns.

        ``live_ids`` is the set of runs an in-memory owner still tracks (e.g.
        the load coordinator's active runs) — those are left alone. Every other
        ``pending``/``running`` row is finalized with ``status`` and a short
        summary noting why. Returns the ids that were reconciled."""
        closed: list[str] = []
        for row in self.unfinished(label_prefix=label_prefix):
            rid = row["id"]
            if rid in live_ids:
                continue
            self.finish(rid, status, {"reconciled": True, "reason": reason,
                                      "previous_status": row["status"]})
            closed.append(rid)
        return closed

    def list(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC"
        ).fetchall()
        items = []
        for r in rows:
            summary = json.loads(r["summary"]) if r["summary"] else None
            items.append({
                "id": r["id"],
                "status": r["status"],
                "environment": r["environment"],
                "label": r["label"],
                "project_id": r["project_id"],
                "scenario_count": r["scenario_count"],
                "created_at": r["created_at"],
                "started_at": r["started_at"],
                "completed_at": r["completed_at"],
                "scenarios": _counts(summary),
            })
        return items

    def get(self, run_id: str) -> dict | None:
        r = self._conn.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if r is None:
            return None
        return {
            "id": r["id"],
            "status": r["status"],
            "environment": r["environment"],
            "label": r["label"],
            "project_id": r["project_id"],
            "scenario_count": r["scenario_count"],
            "created_at": r["created_at"],
            "started_at": r["started_at"],
            "completed_at": r["completed_at"],
            "summary": json.loads(r["summary"]) if r["summary"] else None,
        }

    def trend(self, days: int = 30, label: str | None = None) -> list[dict]:
        """Per-day run-outcome trend (the most recent ``days`` buckets with data).

        Each bucket: number of runs, how many passed/failed, and the aggregate
        scenario pass-rate that day — for plotting regression health over time.
        """
        q = "SELECT created_at, status, summary FROM runs WHERE summary IS NOT NULL"
        params: list[Any] = []
        if label:
            q += " AND label = ?"
            params.append(label)
        rows = self._conn.execute(q, params).fetchall()
        buckets: dict[str, dict] = {}
        for r in rows:
            day = (r["created_at"] or "")[:10]  # YYYY-MM-DD
            if not day:
                continue
            b = buckets.setdefault(day, {
                "date": day, "runs": 0, "passed": 0, "failed": 0,
                "scenarios_total": 0, "scenarios_passed": 0,
            })
            b["runs"] += 1
            if r["status"] == "passed":
                b["passed"] += 1
            elif r["status"] in ("failed", "error", "partial"):
                b["failed"] += 1
            counts = _counts(json.loads(r["summary"]))
            b["scenarios_total"] += counts["total"]
            b["scenarios_passed"] += counts["passed"]
        out = []
        for day in sorted(buckets)[-days:]:
            b = buckets[day]
            b["pass_rate"] = (
                round(b["scenarios_passed"] / b["scenarios_total"], 4)
                if b["scenarios_total"] else None
            )
            out.append(b)
        return out

    def flakiness(
        self, days: int = 30, label: str | None = None, min_runs: int = 3,
    ) -> list[dict]:
        """Per-scenario flakiness across recent runs.

        Walks each run's per-scenario outcomes in time order and, for every
        scenario, builds its sequence of pass/fail results. A scenario is *flaky*
        only if it both passed and failed within the window — a scenario that is
        always-pass or always-fail is stable (the latter is a regression, not
        flakiness, and shows up elsewhere). ``flips`` counts how often the result
        changed between consecutive runs; ``score`` = flips / (runs-1) in 0..1, so
        an alternating pass/fail/pass/fail scores 1.0 and a single blip scores low.
        Only scenarios seen at least ``min_runs`` times are considered.
        """
        q = "SELECT created_at, summary FROM runs WHERE summary IS NOT NULL"
        params: list[Any] = []
        if label:
            q += " AND label = ?"
            params.append(label)
        q += " ORDER BY created_at ASC"
        rows = self._conn.execute(q, params).fetchall()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        seq: dict[str, dict] = {}
        for r in rows:
            day = (r["created_at"] or "")[:10]
            if day and day < cutoff:
                continue
            try:
                summary = json.loads(r["summary"])
            except (TypeError, ValueError):
                continue
            for s in summary.get("scenarios", []) or []:
                st = s.get("status")
                if st not in ("passed", "failed"):
                    continue  # blocked/other don't signal flakiness
                key = s.get("scenario_id") or s.get("name") or "?"
                e = seq.setdefault(key, {
                    "scenario_id": s.get("scenario_id") or "",
                    "name": s.get("name") or key,
                    "outcomes": [],
                })
                e["outcomes"].append(st)

        out: list[dict] = []
        for e in seq.values():
            outs = e["outcomes"]
            n = len(outs)
            if n < min_runs:
                continue
            passed = outs.count("passed")
            failed = outs.count("failed")
            if passed == 0 or failed == 0:
                continue  # stable: always-pass or always-fail, not flaky
            flips = sum(1 for a, b in zip(outs, outs[1:]) if a != b)
            out.append({
                "scenario_id": e["scenario_id"],
                "name": e["name"],
                "runs": n,
                "passed": passed,
                "failed": failed,
                "flips": flips,
                "score": round(flips / (n - 1), 4) if n > 1 else 0.0,
                "last_status": outs[-1],
            })
        out.sort(key=lambda x: (x["score"], x["failed"]), reverse=True)
        return out

    def previous(self, run_id: str) -> str | None:
        """The id of the run to diff against: the most recent completed run
        before this one, preferring the same label (like-for-like)."""
        cur = self._conn.execute(
            "SELECT created_at, label FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if cur is None:
            return None
        row = self._conn.execute(
            "SELECT id FROM runs WHERE created_at < ? AND label = ? "
            "AND summary IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (cur["created_at"], cur["label"]),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT id FROM runs WHERE created_at < ? AND summary IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (cur["created_at"],),
            ).fetchone()
        return row["id"] if row else None

    def previous_load(self, run_id: str) -> str | None:
        """The load run to diff against: the most recent finished load run before
        this one, preferring the same label (like-for-like). Load runs carry a
        ``load:`` label prefix, so this stays within the load-run family rather
        than falling back to a functional run the way ``previous`` would."""
        cur = self._conn.execute(
            "SELECT created_at, label FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if cur is None:
            return None
        row = self._conn.execute(
            "SELECT id FROM runs WHERE created_at < ? AND label = ? "
            "AND summary IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (cur["created_at"], cur["label"]),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT id FROM runs WHERE created_at < ? AND summary IS NOT NULL "
                "AND label LIKE 'load:%' ORDER BY created_at DESC LIMIT 1",
                (cur["created_at"],),
            ).fetchone()
        return row["id"] if row else None

    def close(self) -> None:
        self._conn.close()
