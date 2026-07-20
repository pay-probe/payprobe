"""SQLite persistence for the insight service.

Follows the house pattern: file-backed beside the other registries
(``INSIGHT_DB``), ``:memory:`` in tests. Three tables:

* ``failures``          — the categorized failure corpus (normalized text only).
* ``scenario_outcomes`` — per-run per-scenario results, the prediction history.
* ``predictions_log``   — every emitted prediction, joined later with the
                          actual outcome: the self-scoring loop (ADR-0005 §4).
* ``categories``        — discovered/renamed category labels (the ONE write
                          surface, and it writes only to this store).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL DEFAULT '',
    scenario_name TEXT NOT NULL DEFAULT '',
    step_id TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    norm TEXT NOT NULL,
    feats TEXT NOT NULL DEFAULT '',
    weight INTEGER NOT NULL DEFAULT 1,
    signature TEXT NOT NULL,
    heuristic TEXT NOT NULL,
    category TEXT NOT NULL,
    model_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, scenario_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_failures_sig ON failures (signature);
CREATE TABLE IF NOT EXISTS scenario_outcomes (
    run_id TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, scenario_id)
);
CREATE TABLE IF NOT EXISTS predictions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT '',
    p_fail REAL NOT NULL,
    basis TEXT NOT NULL,
    model_version TEXT NOT NULL,
    n_history INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    actual TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    heuristic TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_health (
    ts TEXT NOT NULL,
    corpus_size INTEGER NOT NULL,
    novel_share REAL NOT NULL,
    mean_confidence REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'categorizer',
    row_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_rows (
    dataset_id TEXT NOT NULL,
    text TEXT NOT NULL,      -- normalized before storage (PAN-safe)
    label TEXT NOT NULL,
    FOREIGN KEY (dataset_id) REFERENCES datasets (id)
);
CREATE INDEX IF NOT EXISTS idx_dsrows ON dataset_rows (dataset_id);
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'categorizer',
    dataset_ids TEXT NOT NULL,   -- JSON list
    metrics TEXT NOT NULL,       -- JSON (holdout accuracy, labels, sizes)
    artifact TEXT NOT NULL,      -- file path of the pickled model
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InsightStore:
    def __init__(self, path: str | None = None) -> None:
        self._path = path or os.environ.get("INSIGHT_DB", ":memory:")
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # defensive migrations for DBs created before these columns
            for ddl in ("ALTER TABLE failures ADD COLUMN feats TEXT NOT NULL DEFAULT ''",
                        "ALTER TABLE failures ADD COLUMN weight INTEGER NOT NULL DEFAULT 1"):
                try:
                    self._conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()

    # -- failures corpus ------------------------------------------------

    def add_failure(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO failures (run_id, scenario_id, "
                "scenario_name, step_id, target, action, norm, feats, weight, "
                "signature, heuristic, category, model_version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["run_id"], rec.get("scenario_id", ""),
                 rec.get("scenario_name", ""), rec.get("step_id", ""),
                 rec.get("target", ""), rec.get("action", ""),
                 rec["norm"], rec.get("feats", ""),
                 int(rec.get("weight", 1)), rec["signature"],
                 rec["heuristic"], rec["category"], rec["model_version"],
                 rec.get("created_at") or _now()),
            )
            self._conn.commit()

    def failures_for_run(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM failures WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def similar_failures(self, sig: str, exclude_run: str = "",
                         limit: int = 5) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_id, scenario_id, scenario_name, step_id, category, "
            "created_at FROM failures WHERE signature = ? AND run_id != ? "
            "ORDER BY created_at DESC LIMIT ?",
            (sig, exclude_run, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def corpus(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, norm, feats, signature, heuristic, category, "
            "model_version FROM failures"
        ).fetchall()
        return [dict(r) for r in rows]

    def corpus_size(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0]

    def relabel(self, failure_id: int, category: str, model_version: str) -> None:
        """Append-only in spirit: the old label lives on in prior model
        versions' analytics; the row carries the newest label + its version."""
        with self._lock:
            self._conn.execute(
                "UPDATE failures SET category = ?, model_version = ? WHERE id = ?",
                (category, model_version, failure_id))
            self._conn.commit()

    def relabel_signature(self, sig: str, category: str,
                          model_version: str) -> int:
        """Relabel EVERY failure sharing a normalized shape (bulk correction
        from the label queue). Returns the number of rows touched."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE failures SET category = ?, model_version = ? "
                "WHERE signature = ?", (category, model_version, sig))
            self._conn.commit()
            return cur.rowcount

    def signature_groups(self) -> list[dict]:
        """Distinct failure shapes with impact counts and a representative
        row — the unit of active-learning labeling (label one shape, cover
        every instance). Human-corrected shapes are excluded: they're done."""
        rows = self._conn.execute(
            "SELECT signature, SUM(weight) AS count, MIN(id) AS rep_id, "
            "MAX(created_at) AS last_seen, "
            "SUM(CASE WHEN model_version = 'human-correction' THEN 1 ELSE 0 END)"
            " AS corrected FROM failures GROUP BY signature"
        ).fetchall()
        out = []
        for r in rows:
            if r["corrected"]:
                continue
            rep = self._conn.execute(
                "SELECT * FROM failures WHERE id = ?", (r["rep_id"],)).fetchone()
            d = dict(rep)
            d["count"] = r["count"]
            d["last_seen"] = r["last_seen"]
            out.append(d)
        return out

    def category_counts(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT category, heuristic, SUM(weight) AS count, "
            "MAX(created_at) AS last_seen FROM failures "
            "GROUP BY category ORDER BY count DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            named = self._conn.execute(
                "SELECT label FROM categories WHERE id = ?", (d["category"],)
            ).fetchone()
            d["label"] = named["label"] if named else d["category"]
            out.append(d)
        return out

    # -- categories (names) ----------------------------------------------

    def name_category(self, cat_id: str, label: str, heuristic: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO categories (id, label, heuristic, created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET label = ?",
                (cat_id, label, heuristic, _now(), label))
            self._conn.commit()

    def category_label(self, cat_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT label FROM categories WHERE id = ?", (cat_id,)).fetchone()
        return row["label"] if row else None

    # -- outcomes (prediction history) ------------------------------------

    def add_outcome(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO scenario_outcomes (run_id, scenario_id, "
                "name, environment, status, duration_ms, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (rec["run_id"], rec["scenario_id"], rec.get("name", ""),
                 rec.get("environment", ""), rec["status"],
                 int(rec.get("duration_ms", 0)),
                 rec.get("created_at") or _now()),
            )
            self._conn.commit()

    def outcomes(self, scenario_id: str | None = None,
                 environment: str | None = None) -> list[dict]:
        q = ("SELECT * FROM scenario_outcomes WHERE 1=1")
        params: list[Any] = []
        if scenario_id:
            q += " AND scenario_id = ?"
            params.append(scenario_id)
        if environment:
            q += " AND environment = ?"
            params.append(environment)
        q += " ORDER BY created_at ASC"
        return [dict(r) for r in self._conn.execute(q, params).fetchall()]

    def scenario_ids(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT scenario_id, MAX(name) AS name FROM scenario_outcomes "
            "GROUP BY scenario_id").fetchall()
        return [(r["scenario_id"], r["name"]) for r in rows]

    # -- predictions log (self-scoring) ------------------------------------

    def log_prediction(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO predictions_log (scenario_id, environment, p_fail, "
                "basis, model_version, n_history, created_at) VALUES (?,?,?,?,?,?,?)",
                (rec["scenario_id"], rec.get("environment", ""),
                 float(rec["p_fail_next"]), rec["basis"], rec["model_version"],
                 int(rec["n_history"]), _now()))
            self._conn.commit()

    def score_predictions(self, actuals: dict[str, str],
                          run_created_at: str | None = None) -> None:
        """Fill in ``actual`` for logged predictions once outcomes are known.

        ``run_created_at`` scopes the join to predictions made BEFORE that
        run: because ingest processes runs chronologically, every prediction
        is scored against the FIRST subsequent outcome — not whatever run
        happened to be ingested last, which flattered stale predictions."""
        with self._lock:
            for scenario_id, status in actuals.items():
                if run_created_at:
                    self._conn.execute(
                        "UPDATE predictions_log SET actual = ? "
                        "WHERE scenario_id = ? AND actual IS NULL "
                        "AND created_at < ?",
                        (status, scenario_id, run_created_at))
                else:
                    self._conn.execute(
                        "UPDATE predictions_log SET actual = ? "
                        "WHERE scenario_id = ? AND actual IS NULL",
                        (status, scenario_id))
            self._conn.commit()

    def brier(self) -> float | None:
        """Brier score over scored predictions (lower is better); None if none."""
        rows = self._conn.execute(
            "SELECT p_fail, actual FROM predictions_log WHERE actual IS NOT NULL"
        ).fetchall()
        if not rows:
            return None
        total = sum((r["p_fail"] - (1.0 if r["actual"] == "failed" else 0.0)) ** 2
                    for r in rows)
        return round(total / len(rows), 4)

    def brier_trend(self, days: int = 30) -> list[dict]:
        """Daily-bucketed Brier over scored predictions (the calibration
        trend): [{day, brier, n, basis_model_share}], oldest first. ``days``
        caps the window; days with no scored predictions are simply absent."""
        rows = self._conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, p_fail, actual, basis "
            "FROM predictions_log WHERE actual IS NOT NULL "
            "AND created_at >= datetime('now', ?) ORDER BY created_at",
            (f"-{max(1, days)} days",)).fetchall()
        by_day: dict[str, list] = {}
        for r in rows:
            by_day.setdefault(r["day"], []).append(r)
        out: list[dict] = []
        for day, recs in sorted(by_day.items()):
            total = sum(
                (r["p_fail"] - (1.0 if r["actual"] == "failed" else 0.0)) ** 2
                for r in recs)
            model_n = sum(1 for r in recs if r["basis"] == "model")
            out.append({"day": day, "brier": round(total / len(recs), 4),
                        "n": len(recs),
                        "basis_model_share": round(model_n / len(recs), 2)})
        return out

    # -- datasets (user-provided training data) -------------------------------

    def create_dataset(self, ds_id: str, name: str, kind: str,
                       rows: list[dict]) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT INTO datasets (id, name, kind, row_count, created_at) "
                "VALUES (?,?,?,?,?)",
                (ds_id, name, kind, len(rows), _now()))
            self._conn.executemany(
                "INSERT INTO dataset_rows (dataset_id, text, label) VALUES (?,?,?)",
                [(ds_id, r["text"], r["label"]) for r in rows])
            self._conn.commit()
        return self.get_dataset(ds_id)  # type: ignore[return-value]

    def list_datasets(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
        out = [dict(r) for r in rows]
        if out:
            # One grouped query for every dataset's label breakdown — the
            # portal (Model Studio cards + wizard review) needs it in the
            # list, not only on the detail read.
            counts = self._conn.execute(
                "SELECT dataset_id, label, COUNT(*) AS n FROM dataset_rows "
                "GROUP BY dataset_id, label ORDER BY n DESC").fetchall()
            by_ds: dict[str, dict[str, int]] = {}
            for r in counts:
                by_ds.setdefault(r["dataset_id"], {})[r["label"]] = r["n"]
            for d in out:
                d["labels"] = by_ds.get(d["id"], {})
        return out

    def get_dataset(self, ds_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM datasets WHERE id = ?", (ds_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        labels = self._conn.execute(
            "SELECT label, COUNT(*) AS n FROM dataset_rows "
            "WHERE dataset_id = ? GROUP BY label ORDER BY n DESC",
            (ds_id,)).fetchall()
        d["labels"] = {r["label"]: r["n"] for r in labels}
        return d

    def dataset_rows(self, ds_ids: list[str]) -> list[dict]:
        if not ds_ids:
            return []
        marks = ",".join("?" for _ in ds_ids)
        rows = self._conn.execute(
            f"SELECT text, label FROM dataset_rows WHERE dataset_id IN ({marks})",
            ds_ids).fetchall()
        return [dict(r) for r in rows]

    def dataset_rows_page(self, ds_id: str, *, label: str | None = None,
                          q: str | None = None, limit: int = 50,
                          offset: int = 0) -> tuple[list[dict], int]:
        """One dataset's rows for the portal viewer: optional label filter +
        substring search, paginated. Returns (rows, total-after-filters)."""
        where = "dataset_id = ?"
        args: list = [ds_id]
        if label:
            where += " AND label = ?"
            args.append(label)
        if q:
            where += " AND text LIKE ?"
            args.append(f"%{q}%")
        total = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM dataset_rows WHERE {where}",
            args).fetchone()["n"]
        rows = self._conn.execute(
            f"SELECT rowid AS id, text, label FROM dataset_rows WHERE {where} "
            "ORDER BY rowid LIMIT ? OFFSET ?",
            [*args, limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    def delete_dataset_row(self, ds_id: str, row_id: int) -> bool:
        """Remove one row (viewer edit); keeps row_count consistent."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM dataset_rows WHERE dataset_id = ? AND rowid = ?",
                (ds_id, row_id))
            if cur.rowcount:
                self._conn.execute(
                    "UPDATE datasets SET row_count = (SELECT COUNT(*) FROM "
                    "dataset_rows WHERE dataset_id = ?) WHERE id = ?",
                    (ds_id, ds_id))
            self._conn.commit()
            return bool(cur.rowcount)

    def relabel_dataset_row(self, ds_id: str, row_id: int, label: str) -> bool:
        """Change one row's label (viewer edit)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE dataset_rows SET label = ? "
                "WHERE dataset_id = ? AND rowid = ?",
                (label, ds_id, row_id))
            self._conn.commit()
            return bool(cur.rowcount)

    def rename_dataset_label(self, ds_id: str, old: str, new: str) -> int:
        """Bulk-rename a label across one dataset; returns rows changed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE dataset_rows SET label = ? "
                "WHERE dataset_id = ? AND label = ?",
                (new, ds_id, old))
            self._conn.commit()
            return cur.rowcount

    def append_dataset_rows(self, ds_id: str, rows: list[dict]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO dataset_rows (dataset_id, text, label) VALUES (?,?,?)",
                [(ds_id, r["text"], r["label"]) for r in rows])
            self._conn.execute(
                "UPDATE datasets SET row_count = "
                "(SELECT COUNT(*) FROM dataset_rows WHERE dataset_id = ?) "
                "WHERE id = ?", (ds_id, ds_id))
            self._conn.commit()

    def delete_dataset(self, ds_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM dataset_rows WHERE dataset_id = ?",
                               (ds_id,))
            self._conn.execute("DELETE FROM datasets WHERE id = ?", (ds_id,))
            self._conn.commit()

    # -- model registry (custom trained artifacts) -----------------------------

    def register_model(self, rec: dict) -> None:
        import json as _json
        with self._lock:
            self._conn.execute(
                "INSERT INTO models (id, name, kind, dataset_ids, metrics, "
                "artifact, active, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (rec["id"], rec["name"], rec.get("kind", "categorizer"),
                 _json.dumps(rec.get("dataset_ids", [])),
                 _json.dumps(rec.get("metrics", {})), rec["artifact"],
                 1 if rec.get("active") else 0, _now()))
            self._conn.commit()

    def list_models(self) -> list[dict]:
        import json as _json
        rows = self._conn.execute(
            "SELECT * FROM models ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["dataset_ids"] = _json.loads(d["dataset_ids"])
            d["metrics"] = _json.loads(d["metrics"])
            d["active"] = bool(d["active"])
            out.append(d)
        return out

    def get_model(self, mid: str) -> dict | None:
        return next((m for m in self.list_models() if m["id"] == mid), None)

    def activate_model(self, mid: str, kind: str = "categorizer") -> None:
        """Single active model per kind; pass mid='' to deactivate all."""
        with self._lock:
            self._conn.execute(
                "UPDATE models SET active = 0 WHERE kind = ?", (kind,))
            if mid:
                self._conn.execute(
                    "UPDATE models SET active = 1 WHERE id = ?", (mid,))
            self._conn.commit()

    def active_model(self, kind: str = "categorizer") -> dict | None:
        return next((m for m in self.list_models()
                     if m["active"] and m["kind"] == kind), None)

    def delete_model(self, mid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM models WHERE id = ?", (mid,))
            self._conn.commit()

    # -- model health (drift watch) ------------------------------------------

    def add_health_snapshot(self, corpus_size: int, novel_share: float,
                            mean_confidence: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO model_health (ts, corpus_size, novel_share, "
                "mean_confidence) VALUES (?,?,?,?)",
                (_now(), corpus_size, round(novel_share, 4),
                 round(mean_confidence, 4)))
            self._conn.commit()

    def health_snapshots(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM model_health ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = ?", (key, value, value))
            self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
