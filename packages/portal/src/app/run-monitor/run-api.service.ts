import { HttpClient } from "@angular/common/http";
import { Injectable, inject } from "@angular/core";
import { Observable } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import {
  CreateRunRequest,
  CreateRunResponse,
  EnvironmentInfo,
  RunCompare,
  RunDetail,
  RunListItem,
  Signoff,
} from "./run.models";

/** Typed client for the run-orchestrator REST surface (spec §4.2). */
@Injectable({ providedIn: "root" })
export class RunApiService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  /** Resolved per call so Settings → Endpoints overrides take effect live. */
  private get base(): string {
    return this.cfg.runApiBase;
  }

  /** Kick off a run; pair with RunMonitorService.stream(run_id) for live events. */
  start(req: CreateRunRequest = {}): Observable<CreateRunResponse> {
    return this.http.post<CreateRunResponse>(`${this.base}/runs`, req);
  }

  /** Run history registry, newest first. */
  list(): Observable<RunListItem[]> {
    return this.http.get<RunListItem[]>(`${this.base}/runs`);
  }

  /** Full run detail (per-scenario, per-step results) for the report view. */
  get(runId: string): Observable<RunDetail> {
    return this.http.get<RunDetail>(`${this.base}/runs/${runId}`);
  }

  /** Regression diff vs another run (default: previous like-for-like run). */
  compare(runId: string, to?: string): Observable<RunCompare> {
    const url = `${this.base}/runs/${runId}/compare` + (to ? `?to=${to}` : "");
    return this.http.get<RunCompare>(url);
  }

  /** Absolute URL to download this run's JUnit XML. */
  junitUrl(runId: string): string {
    return `${this.base}/runs/${runId}/junit`;
  }

  /** Absolute URL to download this run's self-contained HTML report. */
  htmlUrl(runId: string): string {
    return `${this.base}/runs/${runId}/html`;
  }

  /** Authenticated fetch of a downloadable artifact (HTML report, JUnit,
   *  sign-off doc, …). Plain `<a href>` / `window.open` navigations bypass
   *  the HttpClient auth interceptor, so the orchestrator's fail-closed JWT
   *  gate answers 401 and the browser saves the JSON error ("html.json")
   *  instead of the report. Fetching as a blob keeps the bearer token. */
  fetchBlob(url: string): Observable<Blob> {
    return this.http.get(url, { responseType: "blob" });
  }

  cancel(runId: string): Observable<{ id: string; status: string }> {
    return this.http.post<{ id: string; status: string }>(
      `${this.base}/runs/${runId}/cancel`,
      {},
    );
  }

  // -- step-through debugger --

  debugStep(runId: string) {
    return this.http.post(`${this.base}/runs/${runId}/debug/step`, {});
  }
  debugContinue(runId: string) {
    return this.http.post(`${this.base}/runs/${runId}/debug/continue`, {});
  }
  debugStop(runId: string) {
    return this.http.post(`${this.base}/runs/${runId}/cancel`, {});
  }

  /** Test-fire / step-debug a participant flow against a sample request.
   *  Streams the same run events as a scenario debug run. */
  startFlowDebug(req: {
    flow: Record<string, unknown>;
    request: Record<string, unknown>;
    debug: boolean;
    breakpoints: string[];
    label?: string;
  }): Observable<CreateRunResponse> {
    return this.http.post<CreateRunResponse>(
      `${this.base}/flows/debug-run`,
      req,
    );
  }

  /** Execute a single node (editor "Execute step") with upstream context. */
  executeNode(req: ExecuteNodeRequest): Observable<ExecuteNodeResult> {
    return this.http.post<ExecuteNodeResult>(`${this.base}/nodes/execute`, req);
  }

  /** Bundled environments the Execute picker can run against. */
  environments(): Observable<EnvironmentInfo[]> {
    return this.http.get<EnvironmentInfo[]>(`${this.base}/environments`);
  }

  /** Syntax-check a code snippet (editor diagnostics). */
  validateCode(language: string, code: string): Observable<CodeValidation> {
    return this.http.post<CodeValidation>(`${this.base}/code/validate`, {
      language,
      code,
    });
  }

  // -- regression-health trend --

  trend(days = 30, label?: string): Observable<TrendPoint[]> {
    let url = `${this.base}/runs/trend?days=${days}`;
    if (label) url += `&label=${encodeURIComponent(label)}`;
    return this.http.get<TrendPoint[]>(url);
  }

  flakiness(days = 30, label?: string): Observable<FlakyScenario[]> {
    let url = `${this.base}/runs/flakiness?days=${days}`;
    if (label) url += `&label=${encodeURIComponent(label)}`;
    return this.http.get<FlakyScenario[]>(url);
  }

  // -- scheduled regression runs --

  schedules(): Observable<Schedule[]> {
    return this.http.get<Schedule[]>(`${this.base}/schedules`);
  }
  createSchedule(draft: ScheduleDraft): Observable<Schedule> {
    return this.http.post<Schedule>(`${this.base}/schedules`, draft);
  }
  runSchedule(id: string): Observable<{ run_id: string }> {
    return this.http.post<{ run_id: string }>(
      `${this.base}/schedules/${id}/run`,
      {},
    );
  }
  setScheduleEnabled(id: string, enabled: boolean): Observable<Schedule> {
    return this.http.post<Schedule>(
      `${this.base}/schedules/${id}/enabled?enabled=${enabled}`,
      {},
    );
  }
  deleteSchedule(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/schedules/${id}`);
  }

  // -- certification (run scored against a test-case pack) --

  certification(runId: string, pack: string): Observable<Certification> {
    return this.http.get<Certification>(
      `${this.base}/runs/${runId}/certification?pack=${encodeURIComponent(pack)}`,
    );
  }
  certificationHtmlUrl(runId: string, pack: string): string {
    return `${this.base}/runs/${runId}/certification/html?pack=${encodeURIComponent(pack)}`;
  }

  // -- Go/No-Go sign-off (immutable, gated, provenance-stamped) --

  /** Certify a run: evaluate gates, stamp provenance, freeze a snapshot. */
  certify(
    runId: string,
    body: { pack: string; policy?: Record<string, unknown>; project?: string },
  ): Observable<Signoff> {
    return this.http.post<Signoff>(`${this.base}/runs/${runId}/certify`, body);
  }

  /** Sign-off snapshots, newest first; filterable by run/project/verdict. */
  signoffs(filter?: {
    run_id?: string;
    project?: string;
    verdict?: string;
  }): Observable<{ signoffs: Signoff[] }> {
    const q = new URLSearchParams(
      Object.entries(filter ?? {}).filter(([, v]) => v != null) as [
        string,
        string,
      ][],
    ).toString();
    return this.http.get<{ signoffs: Signoff[] }>(
      `${this.base}/signoffs` + (q ? `?${q}` : ""),
    );
  }

  signoff(sid: string): Observable<Signoff> {
    return this.http.get<Signoff>(`${this.base}/signoffs/${sid}`);
  }

  /** Absolute URL to the printable sign-off document (Print → PDF). */
  signoffHtmlUrl(sid: string): string {
    return `${this.base}/signoffs/${sid}/html`;
  }

  /** Append to a sign-off's approval trail. */
  approveSignoff(
    sid: string,
    body: { decision?: string; note?: string },
  ): Observable<Signoff> {
    return this.http.post<Signoff>(
      `${this.base}/signoffs/${sid}/approve`,
      body,
    );
  }

  // -- host/responder simulators --

  simulators(): Observable<Simulator[]> {
    return this.http.get<Simulator[]>(`${this.base}/simulators`);
  }
  simulator(id: string): Observable<Simulator> {
    return this.http.get<Simulator>(`${this.base}/simulators/${id}`);
  }
  startSimulator(draft: {
    label: string;
    config: unknown;
  }): Observable<Simulator> {
    return this.http.post<Simulator>(`${this.base}/simulators`, draft);
  }
  /** Stop a running simulator and drop it from the active list (keeps the
   *  saved config). */
  stopSimulator(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/simulators/${id}`);
  }
  /** Reset a running simulator's counters, traffic log and timeline. */
  clearSimulator(id: string): Observable<Simulator> {
    return this.http.post<Simulator>(`${this.base}/simulators/${id}/clear`, {});
  }
  /** Live metrics payload (stats + breakdowns + throughput timeline). */
  simulatorMetrics(id: string): Observable<SimulatorMetrics> {
    return this.http.get<SimulatorMetrics>(
      `${this.base}/simulators/${id}/metrics`,
    );
  }

  // -- live chaos dial --
  /** Current chaos block + storm status of a running simulator. */
  simulatorChaos(id: string): Observable<ChaosStatus> {
    return this.http.get<ChaosStatus>(`${this.base}/simulators/${id}/chaos`);
  }
  /** Apply a chaos block to a RUNNING simulator (empty = off, no restart). */
  setSimulatorChaos(id: string, chaos: ChaosBlock): Observable<ChaosStatus> {
    return this.http.put<ChaosStatus>(
      `${this.base}/simulators/${id}/chaos`,
      chaos,
    );
  }
  /** Run a timed chaos sequence (outage / brownout / flapping). */
  startChaosStorm(
    id: string,
    body: { phases: ChaosPhase[]; repeat?: number },
  ): Observable<ChaosStatus> {
    return this.http.post<ChaosStatus>(
      `${this.base}/simulators/${id}/chaos/storm`,
      body,
    );
  }
  /** Cancel a running storm; the pre-storm chaos block is restored. */
  cancelChaosStorm(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/simulators/${id}/chaos/storm`);
  }

  // -- resilience runs (chaos + observation → pass/fail certificate) --
  /** History of resilience runs (newest first, headline verdicts). */
  resilienceRuns(): Observable<ResilienceRunCard[]> {
    return this.http.get<ResilienceRunCard[]>(`${this.base}/resilience/runs`);
  }
  /** Start a resilience run against a target sim + a running load run. */
  startResilienceRun(body: ResilienceRunRequest): Observable<ResilienceRun> {
    return this.http.post<ResilienceRun>(`${this.base}/resilience/runs`, body);
  }
  /** Live/finished resilience run — samples + the scored report when done. */
  resilienceRun(id: string): Observable<ResilienceRun> {
    return this.http.get<ResilienceRun>(`${this.base}/resilience/runs/${id}`);
  }
  /** Cancel a running resilience run; any storm it started is lifted. */
  cancelResilienceRun(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/resilience/runs/${id}`);
  }
  /** Persist a running (ad-hoc) simulator into the saved registry; it keeps
   *  running under the new saved id. */
  saveRunningSimulator(id: string, enabled = true): Observable<SavedSimulator> {
    return this.http.post<SavedSimulator>(
      `${this.base}/simulators/${id}/save?enabled=${enabled}`,
      {},
    );
  }

  // -- live sessions (connected peers) --

  /** Every inbound client connected to a listener we host (simulators +
   *  participant responders). Outbound is reserved for a later cut. */
  peers(): Observable<PeersSnapshot> {
    return this.http.get<PeersSnapshot>(`${this.base}/peers`);
  }

  /** One live graph of the simulated network (drivers → participants →
   *  downstream sims/hosts) for the Topology Map. */
  networkGraph(): Observable<NetworkGraph> {
    return this.http.get<NetworkGraph>(`${this.base}/network-graph`);
  }

  /** Triage a finished run: ranked likely causes + fixes; when the failure is
   *  connectivity-class this also carries a live scoped doctor pass. */
  diagnose(runId: string): Observable<RunDiagnosis> {
    return this.http.get<RunDiagnosis>(`${this.base}/runs/${runId}/diagnose`);
  }

  // -- platform doctor (layered connectivity diagnostics) --

  /** One layered diagnostic pass over the whole stack: services →
   *  connections (config/DNS/live probe) → listeners → recent-run
   *  correlation. Each check carries a status and a concrete fix hint. */
  diagnostics(opts?: {
    layers?: string[];
    connection?: string;
    environment?: string;
  }): Observable<DiagnosticsReport> {
    const params: string[] = [];
    if (opts?.layers?.length) params.push(`layers=${opts.layers.join(",")}`);
    if (opts?.connection)
      params.push(`connection=${encodeURIComponent(opts.connection)}`);
    if (opts?.environment)
      params.push(`environment=${encodeURIComponent(opts.environment)}`);
    const qs = params.length ? `?${params.join("&")}` : "";
    return this.http.get<DiagnosticsReport>(`${this.base}/diagnostics${qs}`);
  }

  // -- proxy capture (transparent tap) --

  /** Recorded (redacted) frames a proxy simulator has relayed. */
  capture(id: string): Observable<CaptureSnapshot> {
    return this.http.get<CaptureSnapshot>(
      `${this.base}/simulators/${id}/capture`,
    );
  }
  clearCapture(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/simulators/${id}/capture`);
  }
  saveCaptureAsScenario(
    id: string,
    body: { name: string; project_id?: string },
  ): Observable<{ scenario_id: string; name: string; steps: number }> {
    return this.http.post<{ scenario_id: string; name: string; steps: number }>(
      `${this.base}/simulators/${id}/capture/save-as-scenario`,
      body,
    );
  }

  // -- saved-simulator registry --

  savedSimulators(): Observable<SavedSimulator[]> {
    return this.http.get<SavedSimulator[]>(`${this.base}/simulator-configs`);
  }
  getSavedSimulator(id: string): Observable<SavedSimulator> {
    return this.http.get<SavedSimulator>(
      `${this.base}/simulator-configs/${id}`,
    );
  }
  createSavedSimulator(draft: {
    label: string;
    config: unknown;
    enabled?: boolean;
  }): Observable<SavedSimulator> {
    return this.http.post<SavedSimulator>(
      `${this.base}/simulator-configs`,
      draft,
    );
  }
  updateSavedSimulator(
    id: string,
    patch: { label?: string; config?: unknown; enabled?: boolean },
  ): Observable<SavedSimulator> {
    return this.http.put<SavedSimulator>(
      `${this.base}/simulator-configs/${id}`,
      patch,
    );
  }
  startSavedSimulator(id: string): Observable<SavedSimulator> {
    return this.http.post<SavedSimulator>(
      `${this.base}/simulator-configs/${id}/start`,
      {},
    );
  }
  stopSavedSimulator(id: string): Observable<SavedSimulator> {
    return this.http.post<SavedSimulator>(
      `${this.base}/simulator-configs/${id}/stop`,
      {},
    );
  }
  setSavedSimulatorEnabled(
    id: string,
    enabled: boolean,
  ): Observable<SavedSimulator> {
    return this.http.post<SavedSimulator>(
      `${this.base}/simulator-configs/${id}/enabled?enabled=${enabled}`,
      {},
    );
  }
  deleteSavedSimulator(id: string): Observable<void> {
    return this.http.delete<void>(`${this.base}/simulator-configs/${id}`);
  }
}

export interface CodeProblem {
  line: number;
  column: number;
  message: string;
}

export interface CodeValidation {
  ok: boolean;
  errors: CodeProblem[];
  skipped?: string;
}

export interface ExecuteNodeRequest {
  node: unknown;
  context?: Record<string, unknown>;
  environment?: Record<string, unknown>;
}

export interface ExecuteNodeResult {
  ok: boolean;
  status: "passed" | "failed" | "error";
  request?: unknown;
  response?: unknown;
  error?: string | null;
  logs?: string;
  assertions?: unknown[];
  duration_ms?: number;
}

export interface TrendPoint {
  date: string;
  runs: number;
  passed: number;
  failed: number;
  scenarios_total: number;
  scenarios_passed: number;
  pass_rate: number | null;
}

/** A scenario that intermittently passes and fails across recent runs. */
export interface FlakyScenario {
  scenario_id: string;
  name: string;
  runs: number;
  passed: number;
  failed: number;
  flips: number;
  /** 0..1 — flips / (runs-1); 1.0 = alternates every run. */
  score: number;
  last_status: "passed" | "failed";
}

export interface ScheduleDraft {
  label: string;
  request: Record<string, unknown>;
  interval_sec?: number | null;
  daily_at?: string | null;
  enabled?: boolean;
}

export interface Schedule extends ScheduleDraft {
  id: string;
  created_at: string;
  last_run_at: string | null;
  next_run_at: string;
}

/** A chaos/fault-injection block (worker.adapters.tcp.chaos schema). */
export interface ChaosBlock {
  seed?: number;
  drop_pct?: number;
  latency_ms?: number | { min: number; max: number; pct?: number };
  malformed_pct?: number;
  malformed_mode?: "garbage" | "bad_mti" | "bad_length" | "flip_bits";
  partial_pct?: number;
  partial_bytes?: number | "half";
}

/** One phase of a chaos storm ({} chaos = calm half of a flap cycle). */
export interface ChaosPhase {
  duration_s: number;
  chaos: ChaosBlock;
  label?: string;
}

/** GET/PUT /simulators/{id}/chaos — the live fault dial + storm status. */
export interface ChaosStatus {
  id: string;
  label?: string;
  chaos: ChaosBlock;
  faults?: number;
  storm: {
    active: boolean;
    phases_total: number;
    repeat: number;
    cycle: number;
    phase_index: number;
    phase: string;
    phase_duration_s: number;
    phase_remaining_s: number;
  } | null;
}

/** POST /resilience/runs body. */
export interface ResilienceRunRequest {
  target_sim_id: string;
  load_run_id: string;
  storm: { phases: ChaosPhase[]; repeat?: number };
  baseline_s?: number;
  recovery_s?: number;
  label?: string;
  thresholds?: Record<string, number>;
}

/** One per-second observation during a resilience run. */
export interface ResilienceSample {
  t: number;
  stage: string;
  sim_received: number;
  sim_faults: number;
  client_received: number;
  client_errors: number;
  p95_ms: number;
  tps: number;
}

/** A hard pass/fail gate in the resilience report. */
export interface ResilienceGate {
  id: string;
  label: string;
  value: number;
  threshold: number;
  passed: boolean;
}

/** Per-stage roll-up in the resilience report. */
export interface ResilienceStage {
  label: string;
  seconds: number;
  client_received: number;
  client_errors: number;
  success_rate: number;
  error_rate: number;
  sim_received: number;
  sim_faults: number;
  fault_rate: number;
  p95_ms: number;
  p95_max_ms: number;
}

/** The scored resilience report (resilience.score_resilience). */
export interface ResilienceReport {
  score: number;
  grade: string;
  verdict: "PASS" | "FAIL";
  components: Record<string, number>;
  weights: Record<string, number>;
  gates: ResilienceGate[];
  blocking: string[];
  stages: Record<string, ResilienceStage>;
  baseline_success_rate: number;
  baseline_p95_ms: number;
  thresholds: Record<string, number>;
  notes: string[];
}

export interface ResilienceRun {
  id: string;
  label?: string;
  status: string;
  stage?: string;
  target_sim_id: string;
  load_run_id: string;
  created_at?: number;
  finished_at?: number | null;
  samples: ResilienceSample[];
  report: ResilienceReport | null;
  error?: string | null;
}

/** Compact resilience-run history card. */
export interface ResilienceRunCard {
  id: string;
  label?: string;
  status: string;
  stage?: string;
  target_sim_id: string;
  created_at?: number;
  score?: number;
  grade?: string;
  verdict?: "PASS" | "FAIL";
}

export interface Simulator {
  id: string;
  label: string;
  protocol: string;
  port: number | null;
  rules: number;
  received: number;
  chaos?: boolean;
  /** A timed chaos storm is currently driving this simulator's fault dial. */
  storm?: boolean;
  faults?: number;
  /** Latest instantaneous requests/sec from the sampler. */
  rps?: number;
  /** Whether a saved config of the same id backs this running responder. */
  saved?: boolean;
  /** True for a capture-capable transparent proxy (kind:"proxy"). */
  proxy?: boolean;
  /** Ad-hoc Playground messages that have hit this simulator (ADR-0007) —
   *  its stats are contaminated for certification when > 0. */
  playground_hits?: number;
  log?: SimulatorLogLine[];
}

export interface SimulatorLogLine {
  mti?: string;
  de?: Record<string, string>;
  header?: string;
  command?: string;
  chaos?: string;
}

/** A saved (persisted) simulator definition + its live running state. */
export interface SavedSimulator {
  id: string;
  label: string;
  config: Record<string, unknown>;
  enabled: boolean;
  protocol: string;
  rules: number;
  chaos: boolean;
  /** True when the config is a transparent proxy (kind:"proxy"). */
  proxy?: boolean;
  created_at?: string;
  updated_at?: string;
  // live state (present when running)
  running: boolean;
  port?: number | null;
  received?: number;
  faults?: number;
  rps?: number;
  /** True for a running ad-hoc simulator (started via MCP/API) that has no
   *  saved config yet — surfaced on the page so a started sim is never hidden. */
  unsaved?: boolean;
  /** Ad-hoc Playground messages that have hit this simulator (ADR-0007). */
  playground_hits?: number;
}

/** One recorded frame in a proxy's capture (sensitive DEs already masked). */
export interface CaptureRow {
  seq: number;
  ts: number;
  direction: "c2u" | "u2c";
  mti?: string;
  values?: Record<string, string>;
  undecoded?: boolean;
  raw?: string;
  /** Rule-based logging: tag / severity / note stamped on matched frames. */
  tag?: string;
  level?: string;
  note?: string;
}

/** GET /simulators/{id}/capture — recorded proxy traffic. */
export interface CaptureSnapshot {
  id: string;
  label: string;
  enabled: boolean;
  /** "all" logs every frame (tagging matches); "matched" logs only matches. */
  mode?: string;
  count: number;
  dropped: number;
  /** Frames not logged because no rule matched (mode="matched"). */
  skipped?: number;
  rows: CaptureRow[];
}

/** One throughput sample on a simulator's timeline. */
export interface SimulatorSample {
  t: number;
  rps: number;
  received: number;
  faults: number;
}

/** GET /simulators/{id}/metrics — live dashboard payload. */
export interface SimulatorMetrics extends Simulator {
  stats: {
    received: number;
    faults: number;
    elapsed_s: number;
    avg_rps: number;
    by_mti: Record<string, number>;
    by_response_code: Record<string, number>;
  };
  samples: SimulatorSample[];
}

/** One live connection in the Live Sessions view (GET /peers). */
export interface Peer {
  direction: "inbound" | "outbound";
  /** Remote address, host:port. */
  peer: string;
  /** Epoch seconds the socket connected. */
  since: number;
  /** Seconds since connect. */
  connected_s: number;
  /** Seconds since the last frame on this socket. */
  idle_s: number;
  /** Frames received on this socket. */
  messages: number;
  /** What hosts the listener: "simulator" | "participant". */
  source: string;
  source_id: string;
  /** Listener label (simulator label / participant flow id). */
  label: string;
  protocol: string;
  local_port?: number | null;
  topology_run?: string | null;
}

/** GET /peers — live inbound/outbound connection snapshot. */
export interface PeersSnapshot {
  inbound: Peer[];
  outbound: Peer[];
  counts: {
    inbound: number;
    outbound: number;
    listeners: number;
    peer_ips: number;
  };
}

/** One ranked finding from run triage (GET /runs/:id/diagnose). */
export interface DiagnosisFinding {
  code: string;
  /** error | warn | info */
  severity: string;
  title: string;
  cause: string;
  suggestion: string;
  evidence?: string;
}

/** GET /runs/:id/diagnose — run triage, optionally with a live doctor pass. */
export interface RunDiagnosis {
  ok: boolean;
  headline: string;
  findings: DiagnosisFinding[];
  /** Present when the failure was connectivity-class: what's broken NOW. */
  doctor?: { status: string; problems: DiagnosticCheck[] };
}

/** One check from the platform doctor (GET /diagnostics). */
export interface DiagnosticCheck {
  id: string;
  /** services | connections | listeners | runs */
  layer: string;
  name: string;
  target: string;
  /** ok | warn | fail | skip | disabled */
  status: string;
  latency_ms: number | null;
  detail: string;
  error: string | null;
  /** Concrete next action when the check isn't ok. */
  hint: string;
  /** Runs-layer only: ids of the affected runs (deep-link to /reports/:id). */
  runs?: string[];
}

/** GET /diagnostics — layered platform connectivity report. */
export interface DiagnosticsReport {
  /** ok | degraded | failing */
  status: string;
  summary: Record<string, number>;
  layers: string[];
  environment: string | null;
  checks: DiagnosticCheck[];
  duration_ms: number;
}

/** A node in the live network graph (GET /network-graph). */
export interface GraphNode {
  id: string;
  /** driver | participant | simulator | host | clients */
  kind: string;
  label: string;
  sublabel?: string;
  protocol?: string;
  port?: number | null;
  /** up | down | degraded | external */
  status: string;
  /** Cumulative messages handled by this node (for live-rate deltas). */
  received: number;
  /** Inbound socket count (listeners only). */
  peers?: number;
  /** Reply volume by response code (DE39) — for the live approval rate. */
  by_rc?: Record<string, number>;
  /** Request volume by MTI / command — traffic mix. */
  by_mti?: Record<string, number>;
  /** Participant nodes: the flow behind the listener (deep-link to the editor). */
  flow_id?: string;
  owner?: string;
  saved?: boolean;
}

/** A directed edge in the live network graph. */
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  /** drive | route | client */
  kind: string;
  label?: string;
}

/** A network-run container — groups the participant nodes it owns. */
export interface GraphContainer {
  id: string;
  label: string;
  members: string[];
  instances?: number;
  /** The network-flow id behind this run — deep-link to /network-flows?id=… */
  network_flow_id?: string;
}

/** GET /network-graph — the whole simulated network in one snapshot. */
export interface NetworkGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  containers?: GraphContainer[];
  generated_at: number;
  counts: {
    initiators?: number;
    topologies?: number;
    participants: number;
    simulators: number;
    hosts: number;
    edges: number;
  };
}

export interface Certification {
  run_id: string;
  pack: string;
  scheme: string;
  label: string;
  cases: {
    id: string;
    requirement: string;
    scenario: string;
    status: string;
    passed: boolean;
  }[];
  passed: number;
  total: number;
  compliance_pct: number;
  certified: boolean;
}
