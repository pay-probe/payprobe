import { HttpClient, HttpParams } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import {
  AssistConfig,
  AssistConfigDraft,
  AssistResult,
  Project,
  ProjectDraft,
  Scenario,
  ScenarioDraft,
  ScenarioSet,
  ScenarioSetDraft,
  ScenarioSummary,
  ScenarioVersionInfo,
  SearchResults,
  DataTable,
  DataTableDraft,
  ManagedTarget,
  MessageFormat,
  MessageFormatDraft,
  InstallPackResult,
  IsoAnalysis,
  IsoBuildResult,
  IsoDiff,
  Pack,
  PackSummary,
  Protocol,
  StarterFlow,
  StarterFlowDraft,
  TargetSpec,
  TestClass,
  ValidationResult,
} from "./scenario.models";

export interface VarsResponse {
  variables: Record<string, unknown>;
  secrets: string[];
}

export interface ScenarioFilter {
  projectId?: string;
  setId?: string;
  testClass?: TestClass;
  q?: string;
}

/**
 * Typed client for the scenario-service REST API.
 * Start the service with: uvicorn api.main:app --port 8000
 */
@Injectable({ providedIn: "root" })
export class ScenarioApiService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  /** Resolved per call so Settings → Endpoints overrides take effect live. */
  private get base(): string {
    return this.cfg.scenarioApiBase;
  }

  catalog(): Observable<TargetSpec[]> {
    return this.http.get<TargetSpec[]>(`${this.base}/catalog`);
  }

  /** All targets with provenance flags, for the Step Types editor. */
  manageCatalog(): Observable<{ targets: ManagedTarget[] }> {
    return this.http.get<{ targets: ManagedTarget[] }>(
      `${this.base}/catalog/manage`,
    );
  }

  saveCatalogTarget(spec: TargetSpec): Observable<TargetSpec> {
    return this.http.put<TargetSpec>(
      `${this.base}/catalog/targets/${encodeURIComponent(spec.target)}`,
      spec,
    );
  }

  deleteCatalogTarget(target: string): Observable<void> {
    return this.http.delete<void>(
      `${this.base}/catalog/targets/${encodeURIComponent(target)}`,
    );
  }

  restoreCatalogTarget(target: string): Observable<TargetSpec[]> {
    return this.http.post<TargetSpec[]>(
      `${this.base}/catalog/targets/${encodeURIComponent(target)}/restore`,
      {},
    );
  }

  // -- message formats (versioned ISO specs) --

  formats(protocol?: Protocol): Observable<MessageFormat[]> {
    let params = new HttpParams();
    if (protocol) params = params.set("protocol", protocol);
    return this.http.get<MessageFormat[]>(`${this.base}/formats`, { params });
  }

  createFormat(draft: MessageFormatDraft): Observable<MessageFormat> {
    return this.http.post<MessageFormat>(`${this.base}/formats`, draft);
  }

  updateFormat(
    id: string,
    draft: MessageFormatDraft,
  ): Observable<MessageFormat> {
    return this.http.put<MessageFormat>(
      `${this.base}/formats/${encodeURIComponent(id)}`,
      draft,
    );
  }

  cloneFormat(
    id: string,
    body: { name?: string; version?: string },
  ): Observable<MessageFormat> {
    return this.http.post<MessageFormat>(
      `${this.base}/formats/${encodeURIComponent(id)}/clone`,
      body,
    );
  }

  deleteFormat(id: string): Observable<void> {
    return this.http.delete<void>(
      `${this.base}/formats/${encodeURIComponent(id)}`,
    );
  }

  // -- scoped variables --

  globalVariables(): Observable<VarsResponse> {
    return this.http.get<VarsResponse>(`${this.base}/variables/global`);
  }

  setGlobalVariables(
    variables: Record<string, unknown>,
    secrets: string[] = [],
  ) {
    return this.http.put<VarsResponse>(`${this.base}/variables/global`, {
      variables,
      secrets,
    });
  }

  scopeVariables(scope: "project" | "set", id: string) {
    return this.http.get<VarsResponse>(
      `${this.base}/variables/${scope}/${encodeURIComponent(id)}`,
    );
  }

  setScopeVariables(
    scope: "project" | "set",
    id: string,
    variables: Record<string, unknown>,
    secrets: string[] = [],
  ) {
    return this.http.put<VarsResponse>(
      `${this.base}/variables/${scope}/${encodeURIComponent(id)}`,
      { variables, secrets },
    );
  }

  resolveVariables(body: {
    project_id?: string | null;
    set_ids?: string[];
    variables?: Record<string, unknown>;
    secret_vars?: string[];
  }): Observable<{
    effective: Record<string, unknown>;
    breakdown: unknown;
    secret_values: string[];
  }> {
    return this.http.post<{
      effective: Record<string, unknown>;
      breakdown: unknown;
      secret_values: string[];
    }>(`${this.base}/variables/resolve`, body);
  }

  // -- global named tables --

  tables(): Observable<DataTable[]> {
    return this.http.get<DataTable[]>(`${this.base}/tables`);
  }

  tablesRuntime(): Observable<Record<string, Record<string, unknown>[]>> {
    return this.http.get<Record<string, Record<string, unknown>[]>>(
      `${this.base}/tables/runtime`,
    );
  }

  saveTable(name: string, draft: DataTableDraft): Observable<DataTable> {
    return this.http.put<DataTable>(
      `${this.base}/tables/${encodeURIComponent(name)}`,
      draft,
    );
  }

  deleteTable(name: string): Observable<void> {
    return this.http.delete<void>(
      `${this.base}/tables/${encodeURIComponent(name)}`,
    );
  }

  // -- starter flows (pre-wired editor palette chains) --

  starterFlows(): Observable<StarterFlow[]> {
    return this.http.get<StarterFlow[]>(`${this.base}/starter-flows`);
  }

  createStarterFlow(draft: StarterFlowDraft): Observable<StarterFlow> {
    return this.http.post<StarterFlow>(`${this.base}/starter-flows`, draft);
  }

  updateStarterFlow(
    id: string,
    draft: StarterFlowDraft,
  ): Observable<StarterFlow> {
    return this.http.put<StarterFlow>(
      `${this.base}/starter-flows/${encodeURIComponent(id)}`,
      draft,
    );
  }

  deleteStarterFlow(id: string): Observable<void> {
    return this.http.delete<void>(
      `${this.base}/starter-flows/${encodeURIComponent(id)}`,
    );
  }

  /** AI assistant: natural-language prompt -> an insertable Starter-Flow chain.
   *  Passing the current steps enables edit/extend mode (wire to existing nodes). */
  assistScenario(
    prompt: string,
    scenario: { id: string; target: string; action: string }[] = [],
  ): Observable<AssistResult> {
    return this.http.post<AssistResult>(`${this.base}/scenarios/assist`, {
      prompt,
      scenario,
    });
  }

  /** AI-assistant LLM settings (key returned only as a masked hint). */
  getAssistConfig(): Observable<AssistConfig> {
    return this.http.get<AssistConfig>(`${this.base}/assist/config`);
  }

  saveAssistConfig(draft: AssistConfigDraft): Observable<AssistConfig> {
    return this.http.put<AssistConfig>(`${this.base}/assist/config`, draft);
  }

  /** Make a real call to the configured LLM and report success/failure. */
  testAssistConfig(): Observable<{
    ok: boolean;
    enabled: boolean;
    model?: string;
    reply?: string;
    error?: string;
  }> {
    return this.http.post<{
      ok: boolean;
      enabled: boolean;
      model?: string;
      reply?: string;
      error?: string;
    }>(`${this.base}/assist/test`, {});
  }

  // -- test-case packs --

  packs(): Observable<PackSummary[]> {
    return this.http.get<PackSummary[]>(`${this.base}/packs`);
  }
  pack(id: string): Observable<Pack> {
    return this.http.get<Pack>(`${this.base}/packs/${encodeURIComponent(id)}`);
  }
  installPack(id: string): Observable<InstallPackResult> {
    return this.http.post<InstallPackResult>(
      `${this.base}/packs/${encodeURIComponent(id)}/install`,
      {},
    );
  }

  // -- ISO 8583 inspector --

  analyzeIso8583(
    message: string,
    fields?: Record<string, unknown>,
  ): Observable<IsoAnalysis> {
    return this.http.post<IsoAnalysis>(`${this.base}/iso8583/analyze`, {
      message,
      fields: fields ?? null,
    });
  }

  buildIso8583(
    mti: string,
    values: Record<string, string>,
    fields?: Record<string, unknown>,
  ): Observable<IsoBuildResult> {
    return this.http.post<IsoBuildResult>(`${this.base}/iso8583/build`, {
      mti,
      values,
      fields: fields ?? null,
    });
  }

  diffIso8583(
    a: string,
    b: string,
    fields?: Record<string, unknown>,
  ): Observable<IsoDiff> {
    return this.http.post<IsoDiff>(`${this.base}/iso8583/diff`, {
      a,
      b,
      fields: fields ?? null,
    });
  }

  buildTlv(
    nodes: { tag: string; value?: string }[],
  ): Observable<{ hex: string }> {
    return this.http.post<{ hex: string }>(`${this.base}/iso8583/tlv/build`, {
      nodes,
    });
  }

  list(filter: ScenarioFilter = {}): Observable<ScenarioSummary[]> {
    let params = new HttpParams();
    if (filter.projectId) params = params.set("project_id", filter.projectId);
    if (filter.setId) params = params.set("set_id", filter.setId);
    if (filter.testClass) params = params.set("test_class", filter.testClass);
    if (filter.q) params = params.set("q", filter.q);
    return this.http.get<ScenarioSummary[]>(`${this.base}/scenarios`, {
      params,
    });
  }

  get(id: string, version?: number): Observable<Scenario> {
    return version === undefined
      ? this.http.get<Scenario>(`${this.base}/scenarios/${id}`)
      : this.http.get<Scenario>(
          `${this.base}/scenarios/${id}/versions/${version}`,
        );
  }

  create(
    scenario: ScenarioDraft,
    comment = "",
    projectId: string | null = null,
  ): Observable<Scenario> {
    return this.http.post<Scenario>(`${this.base}/scenarios`, {
      scenario,
      comment,
      project_id: projectId,
    });
  }

  update(
    id: string,
    scenario: ScenarioDraft,
    comment = "",
    baseVersion: number | null = null,
  ): Observable<Scenario> {
    return this.http.put<Scenario>(`${this.base}/scenarios/${id}`, {
      scenario,
      comment,
      base_version: baseVersion,
    });
  }

  delete(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/scenarios/${id}`);
  }

  /** Move a scenario to another project (drops old set memberships). */
  move(id: string, projectId: string): Observable<Scenario> {
    return this.http.post<Scenario>(`${this.base}/scenarios/${id}/move`, {
      project_id: projectId,
    });
  }

  /** Copy a scenario within its project, including set memberships. */
  duplicate(id: string): Observable<Scenario> {
    return this.http.post<Scenario>(
      `${this.base}/scenarios/${id}/duplicate`,
      {},
    );
  }

  versions(id: string): Observable<ScenarioVersionInfo[]> {
    return this.http.get<ScenarioVersionInfo[]>(
      `${this.base}/scenarios/${id}/versions`,
    );
  }

  restore(id: string, version: number): Observable<Scenario> {
    return this.http.post<Scenario>(
      `${this.base}/scenarios/${id}/versions/${version}/restore`,
      {},
    );
  }

  validate(draft: ScenarioDraft): Observable<ValidationResult> {
    return this.http.post<ValidationResult>(`${this.base}/validate`, draft);
  }

  /** Download a scenario as a portable JSON or YAML document. */
  export(id: string, format: "json" | "yaml"): Observable<Blob> {
    return this.http.get(`${this.base}/scenarios/${id}/export`, {
      params: { format },
      responseType: "blob",
    });
  }

  /** Create a scenario from a raw JSON or YAML document (file contents). */
  import(content: string, projectId?: string): Observable<Scenario> {
    let params = new HttpParams();
    if (projectId) params = params.set("project_id", projectId);
    return this.http.post<Scenario>(`${this.base}/scenarios/import`, content, {
      headers: { "Content-Type": "text/plain" },
      params,
    });
  }

  // -- projects --

  projects(): Observable<Project[]> {
    return this.http.get<Project[]>(`${this.base}/projects`);
  }

  createProject(draft: ProjectDraft): Observable<Project> {
    return this.http.post<Project>(`${this.base}/projects`, draft);
  }

  updateProject(id: string, draft: ProjectDraft): Observable<Project> {
    return this.http.put<Project>(`${this.base}/projects/${id}`, draft);
  }

  deleteProject(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/projects/${id}`);
  }

  // -- named sets --

  sets(projectId: string): Observable<ScenarioSet[]> {
    return this.http.get<ScenarioSet[]>(
      `${this.base}/projects/${projectId}/sets`,
    );
  }

  createSet(
    projectId: string,
    draft: Partial<ScenarioSetDraft> & { name: string },
  ): Observable<ScenarioSet> {
    return this.http.post<ScenarioSet>(
      `${this.base}/projects/${projectId}/sets`,
      { description: "", scenario_ids: [], ...draft },
    );
  }

  updateSet(id: string, draft: ScenarioSetDraft): Observable<ScenarioSet> {
    return this.http.put<ScenarioSet>(`${this.base}/sets/${id}`, draft);
  }

  deleteSet(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/sets/${id}`);
  }

  addToSet(setId: string, scenarioId: string): Observable<ScenarioSet> {
    return this.http.put<ScenarioSet>(
      `${this.base}/sets/${setId}/scenarios/${scenarioId}`,
      {},
    );
  }

  removeFromSet(setId: string, scenarioId: string): Observable<ScenarioSet> {
    return this.http.delete<ScenarioSet>(
      `${this.base}/sets/${setId}/scenarios/${scenarioId}`,
    );
  }

  // -- global search --

  search(q: string, projectId?: string): Observable<SearchResults> {
    let params = new HttpParams().set("q", q);
    if (projectId) params = params.set("project_id", projectId);
    return this.http.get<SearchResults>(`${this.base}/search`, { params });
  }
}
