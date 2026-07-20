"""Ingestion — turn orchestrator run history into the corpus + outcome tables.

This is the "real work" the feasibility brief called out: the models are easy,
the feature/label extraction is the product. ``sync()`` is incremental (only
runs newer than the last seen are fetched) and idempotent (store upserts).

Run detail shape (orchestrator ``run_store.get``):
    {id, status, environment, label, created_at, ...,
     summary: {scenarios: [{scenario_id, name, status,
                            steps: [{step_id, target, action, status,
                                     duration_ms, error, ...}]}]}}
"""
from __future__ import annotations

from .categorize import LearnedCategorizer, categorize
from .featurize import context_tokens, training_text
from .normalize import normalize, signature
from .predictor_model import PREDICTOR_ARTIFACT, LearnedPredictor
from .rest import OrchestratorReader
from .store import InsightStore

_TERMINAL = ("passed", "failed", "error", "partial")


class Ingestor:
    def __init__(self, store: InsightStore, reader: OrchestratorReader) -> None:
        self.store = store
        self.reader = reader
        self.learned = LearnedCategorizer()
        #: active user-trained SupervisedCategorizer (or None) — outranks
        #: `learned` in categorize(); set by main.py on boot/train/activate.
        self.custom = None
        #: learned outcome predictor; answers only while it beats the
        #: frequency baseline (fit() gates, retrain re-gates).
        self.predictor = LearnedPredictor()

    def sync(self) -> dict:
        """Pull new completed runs; extract failures + outcomes. Also joins
        actual outcomes onto previously logged predictions (self-scoring)."""
        last_seen = self.store.get_meta("last_run_created_at")
        runs = self.reader.list_runs()
        new = [r for r in runs
               if r.get("status") in _TERMINAL
               and (r.get("created_at") or "") > last_seen]
        new.sort(key=lambda r: r.get("created_at") or "")

        ingested = failures = 0
        for card in new:
            detail = self.reader.get_run(card["id"])
            if not detail or not detail.get("summary"):
                continue
            failures += self._ingest_run(detail)
            ingested += 1
            # Score predictions PER RUN, in chronological order, so every
            # prediction meets the FIRST outcome that followed it.
            actuals = {
                (sc.get("scenario_id") or sc.get("name") or "?"): sc["status"]
                for sc in detail["summary"].get("scenarios", []) or []
                if sc.get("status") in ("passed", "failed")
            }
            if actuals:
                self.store.score_predictions(
                    actuals, run_created_at=detail.get("created_at") or None)
            self.store.set_meta("last_run_created_at",
                                detail.get("created_at") or "")
        load_rows = self._sync_load_runs()

        if ingested or load_rows:
            self._record_health()
        return {"runs_ingested": ingested, "failures_added": failures,
                "load_failure_rows": load_rows,
                "corpus_size": self.store.corpus_size()}

    def _sync_load_runs(self) -> int:
        """Load runs keep no raw error text — workers persist bounded
        ``error_categories`` type-counts (see the feature analysis §1.2). So
        each category becomes ONE corpus row carrying its count as ``weight``:
        the taxonomy and label queue see load failure types with honest
        impact, without pretending we have per-transaction text."""
        last_seen = self.store.get_meta("last_load_run_created_at")
        try:
            cards = self.reader.list_load_runs()
        except RuntimeError:
            return 0  # orchestrator reachable for /runs but not /load-runs? skip
        terminal = ("completed", "stopped", "failed", "error", "passed")
        new = [c for c in cards
               if str(c.get("status", "")) in terminal
               and (c.get("created_at") or "") > last_seen]
        new.sort(key=lambda c: c.get("created_at") or "")
        rows = 0
        for card in new:
            detail = self.reader.get_load_run(card["id"]) or {}
            cats = detail.get("error_categories") or {}
            # tolerate both {"cat": n} and [{"category": c, "count": n}]
            if isinstance(cats, list):
                cats = {str(c.get("category", "?")): int(c.get("count", 1))
                        for c in cats if isinstance(c, dict)}
            target = detail.get("target") or detail.get("connection") or ""
            for cat_text, count in cats.items():
                text = str(cat_text).replace("/", " ")
                self.store.add_failure({
                    "run_id": str(card["id"]), "scenario_id": "load-run",
                    "scenario_name": detail.get("label") or str(card["id"]),
                    "step_id": str(cat_text)[:64],
                    "target": str(target),
                    "action": "load",
                    "norm": normalize(text),
                    "feats": context_tokens(
                        extra={"source": "load", "target": target}),
                    "weight": max(1, int(count)),
                    "signature": signature(text),
                    "heuristic": categorize(text)["heuristic"],
                    "category": categorize(text, self.learned,
                                           self.custom)["category"],
                    "model_version": "heuristic-1",
                    "created_at": card.get("created_at") or "",
                })
                rows += 1
            self.store.set_meta("last_load_run_created_at",
                                card.get("created_at") or "")
        return rows

    def _record_health(self) -> None:
        """Drift-watch snapshot: novel share + mean model confidence over the
        most recent slice of the corpus (bounded, so sync stays cheap)."""
        corpus = self.store.corpus()
        if not corpus:
            return
        recent = corpus[-200:]
        cats = [categorize(r["norm"], self.learned, self.custom,
                           feats=r.get("feats", ""))
                for r in recent]
        novel = sum(1 for c in cats if c["novel"]) / len(cats)
        conf = sum(c["confidence"] for c in cats) / len(cats)
        self.store.add_health_snapshot(len(corpus), novel, conf)

    def drift(self) -> dict:
        """Compare the latest health snapshot against the at-fit baseline.
        ``stale`` means the world has moved since the models last trained —
        retrain (or grow the taxonomy) rather than trusting old labels."""
        snaps = self.store.health_snapshots(limit=1)
        base_novel = self.store.get_meta("novel_share_at_fit")
        base_conf = self.store.get_meta("mean_confidence_at_fit")
        if not snaps or not base_novel:
            return {"stale": False, "reason": None, "latest": snaps[0] if snaps else None}
        latest = snaps[0]
        reasons = []
        if latest["novel_share"] > float(base_novel) + 0.10:
            reasons.append(
                f"novel share {latest['novel_share']:.0%} vs "
                f"{float(base_novel):.0%} at last fit")
        if latest["mean_confidence"] < float(base_conf) * 0.85:
            reasons.append(
                f"mean confidence {latest['mean_confidence']:.2f} vs "
                f"{float(base_conf):.2f} at last fit")
        return {"stale": bool(reasons),
                "reason": "; ".join(reasons) or None,
                "latest": latest,
                "at_fit": {"novel_share": float(base_novel),
                           "mean_confidence": float(base_conf)}}

    def _ingest_run(self, detail: dict) -> int:
        run_id = detail["id"]
        env = detail.get("environment") or ""
        created = detail.get("created_at") or ""
        count = 0
        for sc in detail["summary"].get("scenarios", []) or []:
            sid = sc.get("scenario_id") or sc.get("name") or "?"
            if sc.get("status") in ("passed", "failed"):
                self.store.add_outcome({
                    "run_id": run_id, "scenario_id": sid,
                    "name": sc.get("name") or sid, "environment": env,
                    "status": sc["status"],
                    "duration_ms": sum(int(st.get("duration_ms") or 0)
                                       for st in sc.get("steps", []) or []),
                    "created_at": created,
                })
            for st in sc.get("steps", []) or []:
                if st.get("status") not in ("failed", "error"):
                    continue
                error = st.get("error") or _first_failed_assertion(st)
                feats = context_tokens(st, environment=env)
                cat = categorize(error, self.learned, self.custom, feats=feats)
                self.store.add_failure({
                    "run_id": run_id, "scenario_id": sid,
                    "scenario_name": sc.get("name") or sid,
                    "step_id": str(st.get("step_id") or ""),
                    "target": st.get("target") or "",
                    "action": st.get("action") or "",
                    "norm": normalize(error),
                    "feats": feats,
                    "signature": signature(error),
                    "heuristic": cat["heuristic"],
                    "category": cat["category"],
                    "model_version": cat["model_version"],
                    "created_at": created,
                })
                count += 1
        return count

    def train(self) -> dict:
        """(Re)fit the learned categorizer from the stored corpus, then
        relabel the corpus with the new model. Append-only semantics: each row
        carries the version that labelled it."""
        corpus = self.store.corpus()
        fitted = self.learned.fit(corpus)
        relabelled = 0
        if fitted:
            for row in corpus:
                if row.get("model_version") == "human-correction":
                    continue  # an operator's label is ground truth — keep it
                hit = self.learned.predict(
                    training_text(row.get("feats", ""), row["norm"]))
                if hit and hit["category"] != row["category"]:
                    self.store.relabel(row["id"], hit["category"],
                                       hit["model_version"])
                    relabelled += 1
            self.store.set_meta("categorizer_version", self.learned.version)
            # persist the artifact so a restart doesn't lose the fit
            from .custom_models import data_dir, save_cluster_model
            save_cluster_model(self.learned, data_dir())

        # Learned predictor: refit + re-gate against the frequency baseline.
        sequences: dict[str, list[dict]] = {}
        for o in self.store.outcomes():
            sequences.setdefault(o["scenario_id"], []).append(o)
        pred_result = self.predictor.fit(sequences)
        from .custom_models import data_dir as _dd
        directory = _dd()
        if self.predictor.fitted:
            self.predictor.save(directory)
        elif directory is not None:
            # demoted (or never promoted): drop any stale artifact so a
            # restart can't resurrect a model that no longer beats the baseline
            try:
                (directory / PREDICTOR_ARTIFACT).unlink(missing_ok=True)
            except OSError:
                pass

        # Drift baseline: what "healthy" looked like right after this fit.
        self._record_health()
        snap = self.store.health_snapshots(limit=1)
        if snap:
            self.store.set_meta("novel_share_at_fit",
                                str(snap[0]["novel_share"]))
            self.store.set_meta("mean_confidence_at_fit",
                                str(snap[0]["mean_confidence"]))

        return {"fitted": fitted, "corpus_size": len(corpus),
                "relabelled": relabelled,
                "model_version": self.learned.version or "heuristic-1",
                "predictor": pred_result}


def _first_failed_assertion(step: dict) -> str | None:
    for a in step.get("assertions", []) or []:
        if not a.get("passed", True):
            return a.get("message") or a.get("detail") or str(a)
    return None
