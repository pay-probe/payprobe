import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "./runtime-config.service";

/** One categorized failure with its evidence pack (ADR-0005 §2.1–2.2). */
export interface InsightFailure {
  scenario_id: string;
  scenario_name: string;
  step_id: string;
  target: string;
  action: string;
  /** Category id — a heuristic name ("timeout") or a learned "ins-cat-*". */
  category: string;
  label: string;
  heuristic: string;
  confidence: number;
  model_version: string;
  /** No rule anticipated this failure shape — worth a human look. */
  novel: boolean;
  evidence: {
    help: { what: string; fix: string };
    history: { kind: string; text: string };
    similar_failures: { run_id: string; scenario: string; when: string }[];
    recovered_after_similar: boolean;
    signals: Record<string, string>;
    signature: string;
  };
  /** The evidence pack rendered as prose. */
  explanation: string;
}

export interface RunInsights {
  run_id: string;
  failures: InsightFailure[];
  advisory_only: boolean;
}

/** Per-scenario outcome prediction (ADR-0005 §2.3). */
export interface InsightPrediction {
  scenario_id: string;
  name?: string;
  environment: string;
  p_fail_next: number;
  p_flaky: number;
  basis: string;
  model_version: string;
  n_history: number;
  top_factors: string[];
}

export interface InsightStatus {
  status: string;
  advisory_only: boolean;
  corpus_size: number;
  sklearn_available: boolean;
  categorizer: {
    learned: boolean;
    model_version: string;
    custom: { id: string; name: string; version: string } | null;
    drift?: {
      stale: boolean;
      reason: string | null;
      latest?: { novel_share: number; mean_confidence: number } | null;
    };
  };
  predictor: { model_version: string; brier: number | null };
}

/** One label-queue entry: a failure shape worth teaching (active learning). */
export interface LabelQueueEntry {
  signature: string;
  sample_text: string;
  count: number;
  last_seen: string;
  current: {
    category: string;
    label: string;
    confidence: number;
    novel: boolean;
  };
  score: number;
  run_id: string;
  scenario_id: string;
  step_id: string;
}

/** One row of the failure taxonomy (heuristic, learned, or human label). */
export interface InsightCategory {
  /** Category id — heuristic name, learned "ins-cat-*", or a human label. */
  category: string;
  /** Display name (renamed clusters carry their human-given label). */
  label: string;
  heuristic: string;
  count: number;
  last_seen: string;
}

/** An uploaded labeled dataset (custom training data). */
export interface InsightDataset {
  id: string;
  name: string;
  kind: string;
  row_count: number;
  created_at: string;
  labels?: Record<string, number>;
}

/** A registered custom model (persisted artifact + holdout metrics). */
export interface InsightModel {
  id: string;
  name: string;
  kind: string;
  dataset_ids: string[];
  metrics: {
    rows?: number;
    labels?: string[];
    holdout_accuracy?: number | null;
  };
  active: boolean;
  created_at: string;
}

/**
 * Client for the advisory insight service (ADR-0005). The service is an
 * OPTIONAL deployment and every consumer must degrade gracefully: treat any
 * error as "no insights available", never as a page failure. Its output is a
 * model opinion — advisory styling only, never a gate.
 */
@Injectable({ providedIn: "root" })
export class InsightApiService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);

  private get base(): string {
    return this.cfg.insightApiBase;
  }

  status(): Observable<InsightStatus> {
    return this.http.get<InsightStatus>(`${this.base}/status`);
  }

  /** Categorized failures + explanations for one run (ingests on demand). */
  runFailures(runId: string): Observable<RunInsights> {
    return this.http.get<RunInsights>(
      `${this.base}/insights/failures/${encodeURIComponent(runId)}`,
    );
  }

  /** All per-scenario predictions, riskiest first. */
  predictions(environment?: string): Observable<{
    predictions: InsightPrediction[];
    advisory_only: boolean;
  }> {
    const q = environment
      ? `?environment=${encodeURIComponent(environment)}`
      : "";
    return this.http.get<{
      predictions: InsightPrediction[];
      advisory_only: boolean;
    }>(`${this.base}/insights/predictions${q}`);
  }

  /** Incremental ingest + retrain — safe to call any time. */
  train(): Observable<unknown> {
    return this.http.post(`${this.base}/train`, {});
  }

  /** Daily Brier trend over scored predictions (calibration health). */
  calibration(days = 30): Observable<{
    days: number;
    trend: {
      day: string;
      brier: number;
      n: number;
      basis_model_share: number;
    }[];
    overall_brier: number | null;
  }> {
    return this.http.get<{
      days: number;
      trend: {
        day: string;
        brier: number;
        n: number;
        basis_model_share: number;
      }[];
      overall_brier: number | null;
    }>(`${this.base}/insights/predictions/calibration?days=${days}`);
  }

  /** Discovered/heuristic taxonomy with per-category counts. */
  listCategories(): Observable<{
    categories: InsightCategory[];
    corpus_size: number;
  }> {
    return this.http.get<{
      categories: InsightCategory[];
      corpus_size: number;
    }>(`${this.base}/insights/categories`);
  }

  /** Human names a category (the one write — to the insight store only). */
  renameCategory(id: string, label: string): Observable<unknown> {
    return this.http.post(
      `${this.base}/insights/categories/${encodeURIComponent(id)}/rename`,
      { label },
    );
  }

  /** Operator says "this category is wrong — it's X". Appends to the
   *  reserved "Operator corrections" dataset and relabels the failure as
   *  ground truth (human labels outrank every model). */
  correctFailure(body: {
    run_id: string;
    scenario_id: string;
    step_id: string;
    label: string;
    /** relabel every failure sharing this failure's shape (label-queue path) */
    apply_to_signature?: boolean;
  }): Observable<{ dataset_id: string; corrections_total: number }> {
    return this.http.post<{ dataset_id: string; corrections_total: number }>(
      `${this.base}/insights/corrections`,
      body,
    );
  }

  /** Active learning: the failure shapes most worth a human label. */
  labelQueue(limit = 10): Observable<{
    queue: LabelQueueEntry[];
    shapes_total: number;
  }> {
    return this.http.get<{ queue: LabelQueueEntry[]; shapes_total: number }>(
      `${this.base}/insights/label-queue?limit=${limit}`,
    );
  }

  // -- custom training data + models (Model Studio) --------------------------

  listDatasets(): Observable<{ datasets: InsightDataset[] }> {
    return this.http.get<{ datasets: InsightDataset[] }>(
      `${this.base}/datasets`,
    );
  }

  /** Upload labeled rows (JSON) or a CSV document with text,label columns. */
  createDataset(body: {
    name: string;
    rows?: { text: string; label: string }[];
    csv?: string;
  }): Observable<InsightDataset> {
    return this.http.post<InsightDataset>(`${this.base}/datasets`, body);
  }

  getDataset(id: string): Observable<InsightDataset> {
    return this.http.get<InsightDataset>(
      `${this.base}/datasets/${encodeURIComponent(id)}`,
    );
  }

  /** One page of a dataset's rows (viewer): optional label filter + search. */
  datasetRows(
    id: string,
    opts: { label?: string; q?: string; limit?: number; offset?: number } = {},
  ): Observable<{
    dataset_id: string;
    rows: { id: number; text: string; label: string }[];
    total: number;
    limit: number;
    offset: number;
  }> {
    const p = new URLSearchParams();
    if (opts.label) p.set("label", opts.label);
    if (opts.q) p.set("q", opts.q);
    if (opts.limit != null) p.set("limit", String(opts.limit));
    if (opts.offset != null) p.set("offset", String(opts.offset));
    const qs = p.toString();
    return this.http.get<{
      dataset_id: string;
      rows: { id: number; text: string; label: string }[];
      total: number;
      limit: number;
      offset: number;
    }>(
      `${this.base}/datasets/${encodeURIComponent(id)}/rows${qs ? "?" + qs : ""}`,
    );
  }

  /** Viewer edit: drop one training row (row_count follows). */
  deleteDatasetRow(
    dsId: string,
    rowId: number,
  ): Observable<{ deleted: number; dataset: InsightDataset }> {
    return this.http.delete<{ deleted: number; dataset: InsightDataset }>(
      `${this.base}/datasets/${encodeURIComponent(dsId)}/rows/${rowId}`,
    );
  }

  /** Viewer edit: fix one row's label in place. */
  relabelDatasetRow(
    dsId: string,
    rowId: number,
    label: string,
  ): Observable<{ relabeled: number; label: string; dataset: InsightDataset }> {
    return this.http.patch<{
      relabeled: number;
      label: string;
      dataset: InsightDataset;
    }>(`${this.base}/datasets/${encodeURIComponent(dsId)}/rows/${rowId}`, {
      label,
    });
  }

  /** Viewer edit: rename a label across every row of one dataset. */
  renameDatasetLabel(
    dsId: string,
    fromLabel: string,
    toLabel: string,
  ): Observable<{ renamed: number; dataset: InsightDataset }> {
    return this.http.post<{ renamed: number; dataset: InsightDataset }>(
      `${this.base}/datasets/${encodeURIComponent(dsId)}/rename-label`,
      { from_label: fromLabel, to_label: toLabel },
    );
  }

  /** One-shot categorization (test-drive): optionally pin a model_id. */
  inferCategorize(
    text: string,
    modelId?: string,
  ): Observable<{
    label: string;
    category: string;
    heuristic: string;
    confidence: number;
    novel: boolean;
    model_version: string;
  }> {
    return this.http.post<{
      label: string;
      category: string;
      heuristic: string;
      confidence: number;
      novel: boolean;
      model_version: string;
    }>(`${this.base}/infer`, {
      kind: "categorize",
      text,
      model_id: modelId || undefined,
    });
  }

  deleteDataset(id: string): Observable<unknown> {
    return this.http.delete(`${this.base}/datasets/${encodeURIComponent(id)}`);
  }

  listModels(): Observable<{ models: InsightModel[] }> {
    return this.http.get<{ models: InsightModel[] }>(`${this.base}/models`);
  }

  trainModel(body: {
    name: string;
    dataset_ids: string[];
    activate?: boolean;
  }): Observable<InsightModel> {
    return this.http.post<InsightModel>(`${this.base}/models/train`, body);
  }

  /** id = "none" deactivates (back to clusters/heuristics). */
  activateModel(id: string): Observable<unknown> {
    return this.http.post(
      `${this.base}/models/${encodeURIComponent(id)}/activate`,
      {},
    );
  }

  deleteModel(id: string): Observable<unknown> {
    return this.http.delete(`${this.base}/models/${encodeURIComponent(id)}`);
  }
}
