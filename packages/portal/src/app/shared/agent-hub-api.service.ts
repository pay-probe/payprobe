import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "./runtime-config.service";

/** Registry kinds served by agent-hub (ADR-0010 phase 1). */
export type RegistryKind = "agent" | "workflow";

export type AgentMode = "advisor" | "plan" | "full";

export interface AgentTrigger {
  kind: "manual" | "schedule" | "event" | "mcp";
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
}
