import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "./runtime-config.service";

/** Registry kinds served by agent-hub (ADR-0010 phase 1). */
export type RegistryKind = "agent" | "workflow";

export type AgentMode = "advisor" | "plan" | "full";

export interface AgentTrigger {
  kind: "manual" | "schedule" | "event" | "mcp" | "webhook";
  interval_sec?: number | null;
  daily_at?: string | null;
  event?: string | null;
}

/** The versioned, immutable-once-published body of an agent definition. */
export interface AgentSpec {
  role: string;
  instructions: string;
  model?: string | null;
  temperature?: number;
  examples?: Record<string, unknown>[];
  tools: string[];
  mode: AgentMode;
  write_scope?: { projects: string[]; environments: string[] };
  limits?: { max_steps: number; max_tokens: number; wall_clock_s: number };
  budget?: { daily_tokens: number | null; hard_stop: boolean };
  triggers?: AgentTrigger[];
  rbac?: { invoke: string[]; edit: string[] };
}

export interface WorkflowNode {
  id: string;
  type: "agent_task" | "tool" | "condition" | "approval" | "parallel" | "join";
  agent?: string;
  mode?: AgentMode;
  environment?: string;
  reviews?: string;
  tool?: string;
  expr?: string;
  roles?: string[];
  [k: string]: unknown;
}

export interface WorkflowSpec {
  description?: string;
  inputs?: Record<string, string>;
  nodes: WorkflowNode[];
  edges: { from: string; to: string; when?: string | null }[];
}

export type VersionStatus = "draft" | "active" | "superseded" | "retired";
export type DefinitionStatus = "draft" | "active" | "retired";

/** One version row without its spec (as listed on a definition). */
export interface VersionSummary {
  name: string;
  version: number;
  status: VersionStatus;
  spec_sha256: string;
  created_by: string;
  created_at: string;
  published_by: string | null;
  published_at: string | null;
}

export interface VersionDetail<
  S = AgentSpec | WorkflowSpec,
> extends VersionSummary {
  spec: S;
}

/** A definition as listed (no versions, no spec). */
export interface DefinitionSummary {
  kind: RegistryKind;
  name: string;
  owner: string;
  status: DefinitionStatus;
  builtin: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
  active_version: number | null;
  latest_version: number | null;
  /** Present when a version is active. */
  role?: string;
  mode?: AgentMode;
}

/** A definition with its version history and the active spec. */
export interface DefinitionDetail<
  S = AgentSpec | WorkflowSpec,
> extends DefinitionSummary {
  versions: VersionSummary[];
  spec: S | null;
}

export interface ValidationResult {
  name: string;
  version: number;
  valid: boolean;
  problems: string[];
}

export interface RegistryCatalog {
  tools: {
    name: string;
    tier: "read" | "write" | "execute";
    description: string;
  }[];
  modes: AgentMode[];
  node_types: string[];
  trigger_kinds: string[];
}

export interface PauseState {
  paused: boolean;
  by: string | null;
  at: string | null;
}

export interface HubHealth {
  status: string;
  service: string;
  paused: boolean | null;
  schema_version?: number;
}

/** `paused` and `budget_exceeded` can be recorded for a wake that never ran. */
export type HeartbeatStatus =
  | "running"
  | "done"
  | "failed"
  | "budget_exceeded"
  | "timed_out"
  | "cancelled"
  | "paused";

export type WakeKind = "manual" | "schedule" | "event" | "mcp" | "webhook";

/** One heartbeat as listed (`GET /heartbeats`): counts instead of bodies. */
export interface HeartbeatSummary {
  id: string;
  agent: string;
  version: number;
  spec_sha256: string;
  wake: WakeKind;
  invoked_by: string;
  status: HeartbeatStatus;
  tokens_in: number;
  tokens_out: number;
  model: string;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  reverted_at: string | null;
  n_steps: number;
  n_proposed: number;
  n_writes: number;
}

/** An LLM turn: what the model said and which tools it asked for. */
export interface HeartbeatLlmStep {
  n: number;
  kind: "llm";
  text: string;
  tool_calls: { name: string; args: Record<string, unknown> }[];
  usage: { input: number; output: number };
  ms: number;
}

/** One scoped tool dispatch. `proposed` = plan-mode write recorded, not run. */
export interface HeartbeatToolStep {
  n: number;
  kind: "tool";
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  guardrail: boolean;
  proposed: boolean;
  error: string | null;
  truncated: boolean;
  ms: number;
}

export type HeartbeatStep = HeartbeatLlmStep | HeartbeatToolStep;

export interface ProposedCall {
  step: number;
  tool: string;
  args: Record<string, unknown>;
}

/** Full record (`GET /heartbeats/{id}`), also what wake/cancel/revert return. */
export interface HeartbeatDetail extends Omit<
  HeartbeatSummary,
  "n_steps" | "n_proposed" | "n_writes"
> {
  principal: { sub?: string; roles?: string[] };
  input: string;
  steps: HeartbeatStep[];
  proposed: ProposedCall[];
  journal: unknown[];
  result: string | null;
  cancel_requested: boolean;
  /** Set on a 200 from wake when the agent already had a running heartbeat. */
  coalesced?: boolean;
  /** Set on the revert response: how many journal entries were restored. */
  reverted?: number;
}

export interface WakeRequest {
  input?: string;
  version?: number | null;
  wake?: WakeKind;
}

// -- workflow runs (ADR-0010 phase 3) -----------------------------------------

export type RunStatus =
  | "running"
  | "waiting"
  | "done"
  | "failed"
  | "cancelled"
  | "rejected";

export type NodeStatus =
  | "pending"
  | "deferred"
  | "running"
  | "waiting"
  | "done"
  | "skipped"
  | "failed"
  | "cancelled";

/** One node's persisted state inside a run. */
export interface NodeState {
  status: NodeStatus;
  started_at?: string;
  finished_at?: string;
  error?: string;
  /** agent_task */
  agent?: string;
  mode?: AgentMode;
  heartbeat_id?: string;
  plan_id?: string;
  /** approval */
  approval_id?: string;
  /** tool: journalled writes (revertable) */
  journal?: unknown[];
  retry_at?: number;
}

export interface RunSummary {
  id: string;
  workflow: string;
  version: number;
  spec_sha256: string;
  status: RunStatus;
  invoked_by: string;
  error: string | null;
  cancel_requested: boolean;
  started_at: string;
  updated_at: string;
  finished_at: string | null;
  n_done: number;
  n_nodes: number;
}

export interface RunDetail extends Omit<RunSummary, "n_done" | "n_nodes"> {
  inputs: Record<string, unknown>;
  node_states: Record<string, NodeState>;
  results: Record<string, unknown>;
  principal: { sub?: string; roles?: string[] };
}

export type ApprovalStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "expired"
  | "cancelled";

export interface Approval {
  id: string;
  run_id: string;
  node_id: string;
  workflow: string;
  roles: string[];
  context: {
    inputs?: Record<string, unknown>;
    results?: Record<string, unknown>;
    plans?: string[];
  };
  status: ApprovalStatus;
  decided_by: string | null;
  note: string | null;
  requested_at: string;
  expires_at: string | null;
  decided_at: string | null;
}

export interface PlanArtifact {
  id: string;
  run_id: string | null;
  node_id: string | null;
  heartbeat_id: string | null;
  agent: string;
  version: number;
  spec_sha256: string;
  proposed: ProposedCall[];
  result: string | null;
  authored_by: string;
  created_at: string;
}

/** The problems list agent-hub returns on 422 (shape or publish validation). */
export function problemsOf(err: unknown): string[] {
  const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
  if (detail && typeof detail === "object" && "problems" in detail) {
    const p = (detail as { problems?: unknown }).problems;
    if (Array.isArray(p)) return p.map(String);
  }
  if (typeof detail === "string") return [detail];
  if (detail && typeof detail === "object" && "error" in detail) {
    return [String((detail as { error: unknown }).error)];
  }
  return [];
}

/**
 * HTTP client for agent-hub (:8600). Optional deployment: callers degrade to a
 * "not deployed" state when the service isn't reachable. The base resolves
 * from RuntimeConfig on every call so Settings → Endpoints overrides apply
 * without a reload.
 */
@Injectable({ providedIn: "root" })
export class AgentHubApiService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);

  private get base(): string {
    return this.cfg.agentHubApiBase;
  }

  private path(kind: RegistryKind): string {
    return `${this.base}/${kind}s`;
  }

  health(): Observable<HubHealth> {
    return this.http.get<HubHealth>(`${this.base}/health`);
  }

  catalog(): Observable<RegistryCatalog> {
    return this.http.get<RegistryCatalog>(`${this.base}/catalog`);
  }

  pause(): Observable<PauseState> {
    return this.http.get<PauseState>(`${this.base}/pause`);
  }

  setPaused(paused: boolean): Observable<PauseState> {
    return this.http.put<PauseState>(`${this.base}/pause`, { paused });
  }

  list(
    kind: RegistryKind,
    status?: DefinitionStatus,
  ): Observable<DefinitionSummary[]> {
    const q = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.http.get<DefinitionSummary[]>(`${this.path(kind)}${q}`);
  }

  get(kind: RegistryKind, name: string): Observable<DefinitionDetail> {
    return this.http.get<DefinitionDetail>(
      `${this.path(kind)}/${encodeURIComponent(name)}`,
    );
  }

  create(
    kind: RegistryKind,
    name: string,
    spec: AgentSpec | WorkflowSpec,
    owner = "",
  ): Observable<DefinitionDetail> {
    return this.http.post<DefinitionDetail>(this.path(kind), {
      name,
      owner,
      spec,
    });
  }

  version(
    kind: RegistryKind,
    name: string,
    version: number,
  ): Observable<VersionDetail> {
    return this.http.get<VersionDetail>(
      `${this.path(kind)}/${encodeURIComponent(name)}/versions/${version}`,
    );
  }

  addVersion(
    kind: RegistryKind,
    name: string,
    spec: AgentSpec | WorkflowSpec,
  ): Observable<VersionDetail> {
    return this.http.post<VersionDetail>(
      `${this.path(kind)}/${encodeURIComponent(name)}/versions`,
      { spec },
    );
  }

  updateDraft(
    kind: RegistryKind,
    name: string,
    version: number,
    spec: AgentSpec | WorkflowSpec,
  ): Observable<VersionDetail> {
    return this.http.put<VersionDetail>(
      `${this.path(kind)}/${encodeURIComponent(name)}/versions/${version}`,
      { spec },
    );
  }

  validate(
    kind: RegistryKind,
    name: string,
    version: number,
  ): Observable<ValidationResult> {
    return this.http.post<ValidationResult>(
      `${this.path(kind)}/${encodeURIComponent(name)}/versions/${version}/validate`,
      {},
    );
  }

  publish(
    kind: RegistryKind,
    name: string,
    version: number,
  ): Observable<VersionDetail> {
    return this.http.post<VersionDetail>(
      `${this.path(kind)}/${encodeURIComponent(name)}/versions/${version}/publish`,
      {},
    );
  }

  retire(kind: RegistryKind, name: string): Observable<DefinitionDetail> {
    return this.http.post<DefinitionDetail>(
      `${this.path(kind)}/${encodeURIComponent(name)}/retire`,
      {},
    );
  }

  // -- heartbeats (ADR-0010 phase 2) -------------------------------------------

  /** 202 = a fresh `running` row; 200 = refused (paused/budget) or coalesced. */
  wake(name: string, body: WakeRequest = {}): Observable<HeartbeatDetail> {
    return this.http.post<HeartbeatDetail>(
      `${this.base}/agents/${encodeURIComponent(name)}/wake`,
      body,
    );
  }

  heartbeats(agent?: string, limit = 50): Observable<HeartbeatSummary[]> {
    const q = new URLSearchParams({ limit: String(limit) });
    if (agent) q.set("agent", agent);
    return this.http.get<HeartbeatSummary[]>(`${this.base}/heartbeats?${q}`);
  }

  heartbeat(id: string): Observable<HeartbeatDetail> {
    return this.http.get<HeartbeatDetail>(
      `${this.base}/heartbeats/${encodeURIComponent(id)}`,
    );
  }

  cancelHeartbeat(id: string): Observable<HeartbeatDetail> {
    return this.http.post<HeartbeatDetail>(
      `${this.base}/heartbeats/${encodeURIComponent(id)}/cancel`,
      {},
    );
  }

  /** Restores every journalled write, newest first, under the caller's token. */
  revertHeartbeat(id: string): Observable<HeartbeatDetail> {
    return this.http.post<HeartbeatDetail>(
      `${this.base}/heartbeats/${encodeURIComponent(id)}/revert`,
      {},
    );
  }

  // -- workflow runs (ADR-0010 phase 3) ----------------------------------------

  /** 202 with the run after its first advance (may already be `waiting`). */
  runWorkflow(
    name: string,
    inputs: Record<string, unknown>,
    version?: number | null,
  ): Observable<RunDetail> {
    return this.http.post<RunDetail>(
      `${this.base}/workflows/${encodeURIComponent(name)}/run`,
      { inputs, version: version ?? null },
    );
  }

  runs(
    workflow?: string,
    status?: string,
    limit = 50,
  ): Observable<RunSummary[]> {
    const q = new URLSearchParams({ limit: String(limit) });
    if (workflow) q.set("workflow", workflow);
    if (status) q.set("status", status);
    return this.http.get<RunSummary[]>(`${this.base}/runs?${q}`);
  }

  run(id: string): Observable<RunDetail> {
    return this.http.get<RunDetail>(
      `${this.base}/runs/${encodeURIComponent(id)}`,
    );
  }

  cancelRun(id: string): Observable<RunDetail> {
    return this.http.post<RunDetail>(
      `${this.base}/runs/${encodeURIComponent(id)}/cancel`,
      {},
    );
  }

  /** The inbox: `status` defaults to pending on the server; "all" lists every decision. */
  approvals(
    status = "pending",
    run?: string,
    limit = 100,
  ): Observable<Approval[]> {
    const q = new URLSearchParams({ status, limit: String(limit) });
    if (run) q.set("run", run);
    return this.http.get<Approval[]>(`${this.base}/approvals?${q}`);
  }

  approval(id: string): Observable<Approval> {
    return this.http.get<Approval>(
      `${this.base}/approvals/${encodeURIComponent(id)}`,
    );
  }

  decide(
    id: string,
    decision: "approved" | "rejected",
    note = "",
  ): Observable<Approval> {
    return this.http.post<Approval>(
      `${this.base}/approvals/${encodeURIComponent(id)}/decide`,
      { decision, note },
    );
  }

  plans(run?: string, limit = 50): Observable<PlanArtifact[]> {
    const q = new URLSearchParams({ limit: String(limit) });
    if (run) q.set("run", run);
    return this.http.get<PlanArtifact[]>(`${this.base}/plans?${q}`);
  }

  plan(id: string): Observable<PlanArtifact> {
    return this.http.get<PlanArtifact>(
      `${this.base}/plans/${encodeURIComponent(id)}`,
    );
  }
}
