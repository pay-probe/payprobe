/** Event/stream models mirroring the orchestrator's WebSocket payloads. */

export type RunEventType =
  | "run.started"
  | "phase.update"
  | "step.result"
  | "scenario.result"
  | "run.completed"
  | "debug.paused"
  | "debug.resumed"
  | "load.sample"
  | "load.completed";

export type RunStatus =
  | "pending"
  | "running"
  | "passed"
  | "failed"
  | "blocked"
  | "partial"
  | "error"
  | "cancelled";

/** A single frame from `GET /runs/{id}/stream`. `id` is the resume cursor. */
export interface RunEvent<T = Record<string, unknown>> {
  id: string;
  type: RunEventType;
  run_id: string;
  data: T;
  ts: number;
}

/** A single step-status change between two runs. */
export interface RunChange {
  scenario: string;
  step: string;
  from: string | null;
  to: string | null;
  kind: "regressed" | "fixed" | "changed" | "added" | "removed";
}

/** Regression diff of a run vs another (GET /runs/{id}/compare). */
export interface RunCompare {
  run_id: string;
  base_run_id: string | null;
  base_found: boolean;
  summary: {
    regressed: number;
    fixed: number;
    changed: number;
    added: number;
    removed: number;
  };
  changes: RunChange[];
}

export interface PhaseUpdate {
  phase: number;
  status: RunStatus;
  total?: number;
  passed?: number;
  failed?: number;
  blocked?: number;
  components?: number;
}

export interface StepResult {
  scenario: string;
  phase: number;
  step: string;
  target: string;
  action: string;
  status: RunStatus;
  duration_ms: number;
  assertions: unknown[];
  error: string | null;
  /** Named environment this step was pinned to (per-step override), if any. */
  environment_override?: string | null;
}

export interface CreateRunRequest {
  environment?: Record<string, unknown>;
  environment_name?: string;
  scenarios?: Record<string, unknown>[];
  scenario_ids?: string[];
  project_id?: string;
  set_id?: string;
  label?: string;
  debug?: boolean;
  breakpoints?: string[];
  /** Data-driven: fan the scenario out over the rows of this global table. */
  data_table?: string;
  /** Data-driven: fan the scenario out over these inline rows. */
  dataset?: Record<string, unknown>[];
  /** Gate: this network (historical field name) must be live first, else 424. */
  requires_topology?: string;
}

/** A bundled environment the editor's Execute picker can run against. */
export interface EnvironmentInfo {
  name: string;
  label: string;
  description: string;
}

export interface CreateRunResponse {
  run_id: string;
  status: RunStatus;
}

/** Rolled-up scenario outcome counts shown in the registry. */
export interface ScenarioCounts {
  total: number;
  passed: number;
  failed: number;
  blocked: number;
}

/** A row in the run history registry (GET /runs). */
export interface RunListItem {
  id: string;
  status: RunStatus;
  environment: string | null;
  label: string | null;
  /** Project this run belongs to (project/set runs); null for ad-hoc runs. */
  project_id: string | null;
  scenario_count: number;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  scenarios: ScenarioCounts;
}

/** One assertion outcome inside a step. */
export interface AssertionResult {
  field: string;
  operator: string;
  expected: unknown;
  actual: unknown;
  passed: boolean;
}

/** Trace log levels emitted by the engine for a step. */
export type TraceLevel = "info" | "debug" | "wire" | "pass" | "fail" | "error";

/** One structured engine-log line in a step's execution trace. */
export interface TraceEntry {
  level: TraceLevel;
  msg: string;
}

/** A single executed step in a scenario report. */
export interface StepOutcome {
  step_id: string;
  target: string;
  action: string;
  status: RunStatus;
  duration_ms: number;
  request: unknown;
  response: unknown;
  assertions: AssertionResult[];
  error: string | null;
  /** Wall-clock epoch (ms) when the step was dispatched (waterfall layout). */
  started_at_ms?: number;
  /** Adapter wire detail — bytes sent/received, parsed fields. */
  raw_log?: string;
  /** Structured engine-log lines describing the execution process. */
  trace?: TraceEntry[];
  /** Named environment this step was pinned to (per-step override), if any. */
  environment_override?: string | null;
}

/** A scenario (test case) result within a run. */
export interface ScenarioReport {
  scenario_id: string;
  name: string;
  status: RunStatus;
  steps: StepOutcome[];
}

export interface PhaseSummary {
  status: RunStatus;
  total?: number;
  passed?: number;
  failed?: number;
  blocked?: number;
  components?: number;
  failed_targets?: string[];
}

export interface RunSummary {
  run_id: string;
  status: RunStatus;
  phases: Record<string, PhaseSummary>;
  scenarios: ScenarioReport[];
  error?: string;
}

/** One evaluated gate inside a Go/No-Go verdict. */
export interface GateCheck {
  id: string;
  ok: boolean;
  detail: string;
}

/** GET result of evaluating a run against a GatePolicy. */
export interface GateResult {
  verdict: "GO" | "NO_GO";
  gates: GateCheck[];
  blocking: string[];
}

/** The provenance stamp embedded in a sign-off (what was tested against what). */
export interface Provenance {
  run_id: string | null;
  scenario_versions: Record<string, string | null>;
  pack: { id?: string; version?: string } | null;
  environment: string | null;
  endpoints: { target?: string; host?: string; port?: number }[];
  system_under_test: { build?: string; source?: string } | null;
  triggered_by: string | null;
  started_at: string | null;
  completed_at: string | null;
  baseline_run_id: string | null;
}

/** One entry in a sign-off's approval trail. */
export interface Approval {
  by: string;
  decision: string;
  note: string;
  at: string;
}

/** An immutable Go/No-Go sign-off snapshot (POST /runs/{id}/certify). */
export interface Signoff {
  id: string;
  run_id: string;
  project: string | null;
  pack: string | null;
  environment: string | null;
  verdict: "GO" | "NO_GO";
  gate_result: GateResult;
  provenance: Provenance;
  content_hash: string;
  summary: RunSummary | null;
  created_at: string;
  created_by: string;
  frozen: boolean;
  approvals: Approval[];
}

/** Full run detail (GET /runs/{id}) backing the report view. */
export interface RunDetail {
  id: string;
  status: RunStatus;
  environment: string | null;
  label: string | null;
  project_id: string | null;
  scenario_count: number;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  summary: RunSummary | null;
}
