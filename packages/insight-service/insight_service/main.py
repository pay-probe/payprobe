"""PayProbe Insight Service — FastAPI app (ADR-0005).

Advisory only: the model proposes, the registry and the gates decide. Every
endpoint is a read except ``POST /train`` (rebuilds the service's OWN model)
and ``POST /insights/categories/{id}/rename`` (names a cluster in the
service's OWN store). Nothing here can touch registries, runs, or verdicts.

Run:
    RUN_API_URL=http://localhost:8100 \
        uvicorn insight_service.main:app --port 8500
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .auth import require_auth
from .categorize import HAVE_SKLEARN, categorize, category_help
from .custom_models import (
    SupervisedCategorizer,
    data_dir,
    load_cluster_model,
)
from .explain import evidence_pack, render_text
from .featurize import context_tokens
from .ingest import Ingestor
from .normalize import normalize
from .predict import PREDICTOR_VERSION, predict_outcomes
from .predictor_model import LearnedPredictor
from .rest import OrchestratorReader
from .store import InsightStore

log = logging.getLogger("insight")


async def _train_loop(app: FastAPI, interval: float) -> None:
    """Periodic self-training: incremental ingest + refit, forever. Errors are
    logged and swallowed — a down orchestrator must not kill the loop, and the
    next tick simply tries again."""
    while True:
        await asyncio.sleep(interval)
        try:
            ing: Ingestor = app.state.ingestor
            synced = await run_in_threadpool(ing.sync)
            trained = await run_in_threadpool(ing.train)
            log.info("self-train: %s %s", synced, trained)
        except asyncio.CancelledError:  # shutdown
            raise
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.warning("self-train tick failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the periodic self-training loop when configured.

    ``INSIGHT_TRAIN_INTERVAL_SEC`` > 0 enables it (compose default: nightly).
    0/unset = off — training happens only via explicit ``POST /train``."""
    interval = float(os.environ.get("INSIGHT_TRAIN_INTERVAL_SEC", "0") or 0)
    task = (asyncio.create_task(_train_loop(app, interval))
            if interval > 0 else None)
    yield
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="PayProbe Insight Service", version="0.1.0",
              lifespan=lifespan,
              dependencies=[Depends(require_auth)])

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "CORS_ORIGINS", "http://localhost:8080,http://localhost:4200"
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_state() -> None:
    app.state.store = InsightStore()
    app.state.reader = OrchestratorReader()
    app.state.ingestor = Ingestor(app.state.store, app.state.reader)
    _load_artifacts(app)


def _load_artifacts(app: FastAPI) -> None:
    """Reload persisted models at boot: the auto-trained cluster model and
    the active user-trained custom model (registry → pickled artifact)."""
    ing: Ingestor = app.state.ingestor
    if (cluster := load_cluster_model(data_dir())) is not None:
        ing.learned = cluster
    if (pred := LearnedPredictor.load(data_dir())) is not None:
        ing.predictor = pred
    active = app.state.store.active_model()
    if active:
        model = SupervisedCategorizer.load(active["artifact"])
        if model is not None:
            ing.custom = model
        else:  # artifact missing/corrupt — deactivate rather than lie
            app.state.store.activate_model("", kind=active["kind"])
            log.warning("active model %s artifact unloadable — deactivated",
                        active["id"])


_build_state()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict:
    store: InsightStore = app.state.store
    ing: Ingestor = app.state.ingestor
    return {
        "status": "ok",
        "advisory_only": True,
        "corpus_size": store.corpus_size(),
        "sklearn_available": HAVE_SKLEARN,
        "categorizer": {
            "learned": ing.learned.fitted,
            "model_version": ing.learned.version or "heuristic-1",
            "custom": ({"id": ing.custom.model_id, "name": ing.custom.name,
                        "version": ing.custom.version}
                       if ing.custom is not None else None),
            # drift watch: has the failure landscape moved since the last fit?
            "drift": ing.drift(),
        },
        "predictor": {
            "model_version": (ing.predictor.version
                              if ing.predictor.fitted else PREDICTOR_VERSION),
            "learned": ing.predictor.fitted,
            # holdout gate result from the last fit (model vs baseline Brier)
            "gate": ing.predictor.metrics or None,
            # self-scoring: Brier over logged predictions with known outcomes
            "brier": store.brier(),
        },
    }


@app.post("/train")
async def train() -> dict:
    """Incremental ingest + (re)fit the learned categorizer. Safe to call any
    time; also invoked on a schedule externally (the schedule machinery)."""
    ing: Ingestor = app.state.ingestor
    try:
        synced = await run_in_threadpool(ing.sync)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    trained = await run_in_threadpool(ing.train)
    return {"sync": synced, "train": trained}


@app.get("/insights/failures/{run_id}")
async def run_failures(run_id: str) -> dict:
    """Categorized failures + evidence packs for one run. Ingests the run on
    demand if it hasn't been seen yet."""
    store: InsightStore = app.state.store
    ing: Ingestor = app.state.ingestor

    rows = store.failures_for_run(run_id)
    if not rows:
        detail = await run_in_threadpool(_safe_get_run, app.state.reader, run_id)
        if detail is None:
            raise HTTPException(404, "run not found")
        if detail.get("summary"):
            await run_in_threadpool(ing._ingest_run, detail)
            rows = store.failures_for_run(run_id)

    out = []
    for row in rows:
        if row["model_version"] == "human-correction":
            # an operator already named this failure — their label is ground
            # truth and outranks any model's opinion
            cat = {"category": row["category"], "label": row["category"],
                   "heuristic": row["heuristic"], "confidence": 1.0,
                   "model_version": "human-correction", "novel": False}
        else:
            cat = categorize(row["norm"], ing.learned, ing.custom,
                             feats=row.get("feats", ""))
        label = store.category_label(cat["category"]) or cat["label"]
        history = store.outcomes(scenario_id=row["scenario_id"])
        similar = store.similar_failures(row["signature"], exclude_run=run_id)
        pack = evidence_pack({**row, **cat, "label": label}, history, similar)
        out.append({
            "scenario_id": row["scenario_id"],
            "scenario_name": row["scenario_name"],
            "step_id": row["step_id"],
            "target": row["target"],
            "action": row["action"],
            **{**cat, "label": label},
            "evidence": pack,
            "explanation": render_text(pack),
        })
    return {"run_id": run_id, "failures": out, "advisory_only": True}


@app.get("/insights/categories")
async def categories() -> dict:
    store: InsightStore = app.state.store
    return {"categories": store.category_counts(),
            "corpus_size": store.corpus_size()}


class RenameBody(BaseModel):
    label: str


@app.post("/insights/categories/{cat_id}/rename")
async def rename_category(cat_id: str, body: RenameBody) -> dict:
    """A human names a discovered cluster — the one write, to our own store."""
    store: InsightStore = app.state.store
    store.name_category(cat_id, body.label.strip())
    return {"id": cat_id, "label": body.label.strip()}


def _predict(rows: list[dict], scenario_id: str, environment: str) -> dict:
    """Frequency baseline, overlaid by the learned model when (and only
    when) it earned activation by beating that baseline on holdout."""
    pred = predict_outcomes(rows, scenario_id=scenario_id,
                            environment=environment)
    ing: Ingestor = app.state.ingestor
    if ing.predictor.fitted:
        hit = ing.predictor.predict(rows)
        if hit is not None:
            pred["p_fail_next"] = hit["p_fail_next"]
            pred["basis"] = hit["basis"]
            pred["model_version"] = hit["model_version"]
            pred["top_factors"] = pred["top_factors"] + hit["model_factors"]
    return pred


@app.get("/insights/predictions")
async def predictions(environment: str | None = None) -> dict:
    store: InsightStore = app.state.store
    out = []
    for sid, name in store.scenario_ids():
        rows = store.outcomes(scenario_id=sid, environment=environment)
        if not rows:
            continue
        pred = _predict(rows, sid, environment or "")
        pred["name"] = name
        store.log_prediction(pred)
        out.append(pred)
    out.sort(key=lambda p: p["p_fail_next"], reverse=True)
    return {"predictions": out, "advisory_only": True}


@app.get("/insights/predictions/calibration")
async def prediction_calibration(days: int = 30) -> dict:
    """Daily Brier trend over scored predictions — is the predictor getting
    sharper or decaying? (Declared before the {scenario_id} route so
    'calibration' isn't captured as an id.)"""
    store: InsightStore = app.state.store
    days = max(1, min(365, days))
    return {"days": days, "trend": store.brier_trend(days),
            "overall_brier": store.brier(), "advisory_only": True}


@app.get("/insights/predictions/{scenario_id}")
async def prediction(scenario_id: str, environment: str | None = None) -> dict:
    store: InsightStore = app.state.store
    rows = store.outcomes(scenario_id=scenario_id, environment=environment)
    if not rows:
        raise HTTPException(404, "no recorded outcomes for this scenario")
    pred = _predict(rows, scenario_id, environment or "")
    store.log_prediction(pred)
    return pred


# -- inference: the predict-step endpoint ---------------------------------------

class InferBody(BaseModel):
    #: "categorize" (text → category), "explain" (text → category + reason +
    #: fix), or "predict_outcome" (scenario history → p_fail)
    kind: str
    #: categorize / explain: the message/error text to classify
    text: str | None = None
    #: categorize / explain: pin a SPECIFIC registered model instead of the
    #: active chain (a scenario may depend on one exact taxonomy)
    model_id: str | None = None
    #: categorize / explain: optional structured context — target, action,
    #: response_code, environment, duration_ms — turned into feature-channel
    #: tokens so inference sees the same channels training did
    context: dict | None = None
    #: predict_outcome: which scenario's history to predict from
    scenario_id: str | None = None
    environment: str | None = None


def _pinned_model(mid: str) -> "SupervisedCategorizer":
    """Load (and cache) a registered model by id, active or not."""
    cache: dict = getattr(app.state, "model_cache", None) or {}
    app.state.model_cache = cache
    if mid in cache:
        return cache[mid]
    rec = app.state.store.get_model(mid)
    if rec is None:
        raise HTTPException(404, f"no registered model '{mid}'")
    model = SupervisedCategorizer.load(rec["artifact"])
    if model is None:
        raise HTTPException(409, f"model '{mid}' artifact missing/unreadable")
    cache[mid] = model
    return model


def _categorize_for_infer(body: InferBody) -> dict:
    ing: Ingestor = app.state.ingestor
    store: InsightStore = app.state.store
    if not body.text:
        raise HTTPException(422, f"{body.kind} needs `text`")
    custom = _pinned_model(body.model_id) if body.model_id else ing.custom
    feats = context_tokens(extra=body.context) if body.context else ""
    cat = categorize(body.text, ing.learned, custom, feats=feats)
    cat["label"] = store.category_label(cat["category"]) or cat["label"]
    return cat


@app.post("/infer")
async def infer(body: InferBody) -> dict:
    """One-shot inference for the scenario `predict` step (worker `insight`
    adapter). Fast, side-effect-free (nothing is logged as a prediction —
    scenario control flow shouldn't pollute the self-scoring loop). The
    caller's scenario decides what to do with the answer; gates never see it."""
    store: InsightStore = app.state.store
    if body.kind == "categorize":
        return {"kind": "categorize", **_categorize_for_infer(body)}
    if body.kind == "explain":
        cat = _categorize_for_infer(body)
        help_ = category_help(cat["heuristic"])
        return {"kind": "explain", **cat,
                "why": help_["what"], "fix": help_["fix"],
                "explanation": (f"Category: {cat['label']} "
                                f"({cat['heuristic']}). {help_['what']} "
                                f"Suggested fix: {help_['fix']}")}
    if body.kind == "predict_outcome":
        if not body.scenario_id:
            raise HTTPException(422, "predict_outcome needs `scenario_id`")
        rows = store.outcomes(scenario_id=body.scenario_id,
                              environment=body.environment)
        if not rows:
            raise HTTPException(404, "no recorded outcomes for this scenario")
        return {"kind": "predict_outcome",
                **_predict(rows, body.scenario_id, body.environment or "")}
    raise HTTPException(
        422, "kind must be 'categorize', 'explain' or 'predict_outcome'")


# -- datasets: user-provided training data -------------------------------------

class DatasetBody(BaseModel):
    name: str
    kind: str = "categorizer"
    #: labeled rows: [{"text": "...", "label": "..."}]
    rows: list[dict] | None = None
    #: OR a CSV document: header `text,label` (or two unnamed columns)
    csv: str | None = None


def _parse_csv(doc: str) -> list[dict]:
    import csv as _csv
    import io
    rows: list[dict] = []
    reader = _csv.reader(io.StringIO(doc))
    header: list[str] | None = None
    for rec in reader:
        if not rec or not any(c.strip() for c in rec):
            continue
        if header is None:
            lowered = [c.strip().lower() for c in rec]
            if "text" in lowered and "label" in lowered:
                header = lowered
                continue
            header = ["text", "label"]  # headerless two-column CSV
        d = dict(zip(header, (c.strip() for c in rec)))
        if d.get("text") and d.get("label"):
            rows.append({"text": d["text"], "label": d["label"]})
    return rows


@app.post("/datasets")
async def create_dataset(body: DatasetBody) -> dict:
    """Upload a labeled dataset. Text is normalized before storage (digits,
    hosts, ids masked — same PAN-safety rule as the ingested corpus)."""
    raw = body.rows if body.rows is not None else _parse_csv(body.csv or "")
    rows = [{"text": normalize(str(r.get("text", ""))),
             "label": str(r.get("label", "")).strip()}
            for r in raw if r.get("text") and str(r.get("label", "")).strip()]
    if not rows:
        raise HTTPException(422, "no usable rows — need text + label per row "
                                 "(JSON `rows` or CSV with text,label columns)")
    import uuid
    ds_id = f"ds-{uuid.uuid4().hex[:8]}"
    store: InsightStore = app.state.store
    return store.create_dataset(ds_id, body.name.strip() or ds_id,
                                body.kind, rows)


@app.get("/datasets")
async def list_datasets() -> dict:
    store: InsightStore = app.state.store
    return {"datasets": store.list_datasets()}


@app.get("/datasets/{ds_id}")
async def get_dataset(ds_id: str) -> dict:
    store: InsightStore = app.state.store
    ds = store.get_dataset(ds_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    return ds


@app.get("/datasets/{ds_id}/rows")
async def dataset_rows(ds_id: str, label: str | None = None,
                       q: str | None = None, limit: int = 50,
                       offset: int = 0) -> dict:
    """The dataset viewer's page of rows: optional exact-label filter +
    substring search over the (already normalized, PAN-safe) text."""
    store: InsightStore = app.state.store
    if store.get_dataset(ds_id) is None:
        raise HTTPException(404, "dataset not found")
    limit = max(1, min(500, limit))
    rows, total = store.dataset_rows_page(
        ds_id, label=label, q=q, limit=limit, offset=max(0, offset))
    return {"dataset_id": ds_id, "rows": rows, "total": total,
            "limit": limit, "offset": max(0, offset)}


class RowLabelBody(BaseModel):
    label: str


class RenameLabelBody(BaseModel):
    from_label: str
    to_label: str


@app.delete("/datasets/{ds_id}/rows/{row_id}")
async def delete_dataset_row(ds_id: str, row_id: int) -> dict:
    """Viewer edit: drop one bad training row."""
    store: InsightStore = app.state.store
    if store.get_dataset(ds_id) is None:
        raise HTTPException(404, "dataset not found")
    if not store.delete_dataset_row(ds_id, row_id):
        raise HTTPException(404, "row not found")
    return {"deleted": row_id, "dataset": store.get_dataset(ds_id)}


@app.patch("/datasets/{ds_id}/rows/{row_id}")
async def relabel_dataset_row(ds_id: str, row_id: int,
                              body: RowLabelBody) -> dict:
    """Viewer edit: fix one row's label in place."""
    store: InsightStore = app.state.store
    label = body.label.strip()
    if not label:
        raise HTTPException(422, "label must not be empty")
    if store.get_dataset(ds_id) is None:
        raise HTTPException(404, "dataset not found")
    if not store.relabel_dataset_row(ds_id, row_id, label):
        raise HTTPException(404, "row not found")
    return {"relabeled": row_id, "label": label,
            "dataset": store.get_dataset(ds_id)}


@app.post("/datasets/{ds_id}/rename-label")
async def rename_dataset_label(ds_id: str, body: RenameLabelBody) -> dict:
    """Viewer edit: rename a label across every row of one dataset (typo
    repair). Retrain to make an active model pick it up."""
    store: InsightStore = app.state.store
    new = body.to_label.strip()
    if not new:
        raise HTTPException(422, "to_label must not be empty")
    if store.get_dataset(ds_id) is None:
        raise HTTPException(404, "dataset not found")
    changed = store.rename_dataset_label(ds_id, body.from_label, new)
    return {"renamed": changed, "from": body.from_label, "to": new,
            "dataset": store.get_dataset(ds_id)}


@app.delete("/datasets/{ds_id}")
async def delete_dataset(ds_id: str) -> dict:
    store: InsightStore = app.state.store
    if store.get_dataset(ds_id) is None:
        raise HTTPException(404, "dataset not found")
    store.delete_dataset(ds_id)
    return {"deleted": ds_id}


# -- corrections: the human-in-the-loop feedback channel -------------------------

#: reserved dataset every correction lands in; shows up in Model Studio like
#: any other dataset, so retraining on corrections is one checkbox away.
CORRECTIONS_DS = "ds-corrections"


class CorrectionBody(BaseModel):
    run_id: str
    scenario_id: str
    step_id: str
    #: the operator's label for this failure — their taxonomy, ground truth
    label: str
    #: relabel EVERY failure sharing this failure's normalized shape (the
    #: label-queue path: one answer covers all instances)
    apply_to_signature: bool = False


@app.post("/insights/corrections")
async def correct_failure(body: CorrectionBody) -> dict:
    """An operator says 'this failure's category is wrong — it's X'.

    Two effects: (1) the failure's normalized text + corrected label is
    appended to the reserved *Operator corrections* dataset (training food);
    (2) the corpus row is relabeled immediately with model_version
    ``human-correction`` — a human label outranks any model's, and the
    categories/trends views reflect it at once."""
    label = body.label.strip()
    if not label:
        raise HTTPException(422, "label must not be empty")
    store: InsightStore = app.state.store
    row = next((r for r in store.failures_for_run(body.run_id)
                if r["scenario_id"] == body.scenario_id
                and r["step_id"] == body.step_id), None)
    if row is None:
        raise HTTPException(404, "no recorded failure for that run/scenario/step")

    if store.get_dataset(CORRECTIONS_DS) is None:
        store.create_dataset(CORRECTIONS_DS, "Operator corrections",
                             "categorizer", [])
    store.append_dataset_rows(CORRECTIONS_DS,
                              [{"text": row["norm"], "label": label}])
    if body.apply_to_signature:
        touched = store.relabel_signature(row["signature"], label,
                                          "human-correction")
    else:
        store.relabel(row["id"], label, "human-correction")
        touched = 1
    ds = store.get_dataset(CORRECTIONS_DS)
    return {"dataset_id": CORRECTIONS_DS, "label": label,
            "failures_relabelled": touched,
            "corrections_total": ds["row_count"] if ds else 1}


@app.get("/insights/label-queue")
async def label_queue(limit: int = 20) -> dict:
    """Active learning: the failures MOST worth a human label, best first.

    One entry per distinct failure shape (label once, cover every instance);
    already-corrected shapes are excluded. Ranking: novel shapes first (no
    rule anticipated them), then lowest model confidence, weighted by how
    many failures share the shape — so every minute of labeling teaches the
    model as much as possible."""
    import math

    store: InsightStore = app.state.store
    ing: Ingestor = app.state.ingestor
    queue = []
    for rep in store.signature_groups():
        cat = categorize(rep["norm"], ing.learned, ing.custom,
                         feats=rep.get("feats", ""))
        score = ((2.0 if cat["novel"] else 0.0) + (1.0 - cat["confidence"])) \
            * math.log(1 + rep["count"])
        queue.append({
            "signature": rep["signature"],
            "sample_text": rep["norm"],
            "count": rep["count"],
            "last_seen": rep["last_seen"],
            "current": {"category": cat["category"], "label": cat["label"],
                        "confidence": cat["confidence"], "novel": cat["novel"]},
            "score": round(score, 4),
            # a representative instance the corrections endpoint can target
            "run_id": rep["run_id"],
            "scenario_id": rep["scenario_id"],
            "step_id": rep["step_id"],
        })
    queue.sort(key=lambda q: q["score"], reverse=True)
    return {"queue": queue[:max(1, min(limit, 100))],
            "shapes_total": len(queue), "advisory_only": True}


# -- model registry: train / activate custom models -----------------------------

class TrainBody(BaseModel):
    name: str
    dataset_ids: list[str]
    #: activate immediately when training succeeds (default: registered only)
    activate: bool = False


@app.get("/models")
async def list_models() -> dict:
    store: InsightStore = app.state.store
    return {"models": store.list_models()}


@app.post("/models/train")
async def train_model(body: TrainBody) -> dict:
    """Train a custom supervised categorizer from uploaded dataset(s), persist
    the artifact, and register it (with holdout metrics). Advisory like
    everything else — an activated model only changes labels, never verdicts."""
    store: InsightStore = app.state.store
    for ds_id in body.dataset_ids:
        if store.get_dataset(ds_id) is None:
            raise HTTPException(404, f"dataset '{ds_id}' not found")
    rows = store.dataset_rows(body.dataset_ids)
    directory = data_dir()
    if directory is None:
        raise HTTPException(409, "no artifact directory — set INSIGHT_DATA_DIR "
                                 "or a file-backed INSIGHT_DB to train models")
    import uuid
    mid = f"cm-{uuid.uuid4().hex[:8]}"
    model = SupervisedCategorizer(mid, body.name.strip() or mid)
    try:
        metrics = await run_in_threadpool(model.fit, rows)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    artifact = await run_in_threadpool(model.save, directory)
    store.register_model({"id": mid, "name": model.name, "kind": "categorizer",
                          "dataset_ids": body.dataset_ids, "metrics": metrics,
                          "artifact": artifact, "active": False})
    if body.activate:
        store.activate_model(mid)
        app.state.ingestor.custom = model
    return {"id": mid, "name": model.name, "metrics": metrics,
            "artifact": artifact, "active": body.activate}


@app.post("/models/{mid}/activate")
async def activate_model(mid: str) -> dict:
    """Make this model THE custom categorizer (single active per kind).
    ``mid`` = "none" deactivates — back to clusters/heuristics."""
    store: InsightStore = app.state.store
    ing: Ingestor = app.state.ingestor
    if mid == "none":
        store.activate_model("")
        ing.custom = None
        return {"active": None}
    rec = store.get_model(mid)
    if rec is None:
        raise HTTPException(404, "model not found")
    model = SupervisedCategorizer.load(rec["artifact"])
    if model is None:
        raise HTTPException(409, "model artifact missing or unreadable")
    store.activate_model(mid, kind=rec["kind"])
    ing.custom = model
    return {"active": mid}


@app.delete("/models/{mid}")
async def delete_model(mid: str) -> dict:
    store: InsightStore = app.state.store
    ing: Ingestor = app.state.ingestor
    rec = store.get_model(mid)
    if rec is None:
        raise HTTPException(404, "model not found")
    if rec["active"]:
        ing.custom = None
    getattr(app.state, "model_cache", {}).pop(mid, None)  # evict pinned copies
    store.delete_model(mid)
    try:
        os.unlink(rec["artifact"])
    except OSError:
        pass
    return {"deleted": mid}


def _safe_get_run(reader: OrchestratorReader, run_id: str) -> dict | None:
    try:
        return reader.get_run(run_id)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
