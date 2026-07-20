import { CommonModule, DatePipe } from "@angular/common";
import {
  Component,
  ElementRef,
  HostListener,
  OnInit,
  ViewChild,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { ActivatedRoute, Router, RouterLink } from "@angular/router";

import { CodeEditorComponent } from "../../shared/code-editor.component";
import { RefAutocompleteDirective } from "../../shared/ref-autocomplete.directive";
import { IconComponent } from "../../shared/ui/icon.component";
import { ThemeService } from "../../shared/theme.service";
import { UiService } from "../../shared/ui.service";
import {
  ExecuteNodeResult,
  RunApiService,
} from "../../run-monitor/run-api.service";
import { RunMonitorService } from "../../run-monitor/run-monitor.service";
import {
  EnvironmentInfo,
  RunStatus,
  StepResult,
} from "../../run-monitor/run.models";
import { ConnectionsService } from "../../connections/connections.service";
import { GroupsService } from "../../groups/groups.service";
import { TestDataService } from "../../test-data/test-data.service";
import {
  ParticipantFlow,
  ParticipantsService,
} from "../../participants/participants.service";
import { FLOW_NODE_KINDS } from "../scenario.models";
import { ScenarioApiService } from "../scenario-api.service";
import {
  AUTO_GAP,
  AUTO_X,
  AUTO_Y,
  autoPos,
  bezier,
  defaultConfig,
  rewriteFlowRefs,
  sequentialEdges,
  snap,
} from "./graph-util";
import {
  ActionSpec,
  AssistResult,
  Assertion,
  CODE_LANGUAGES,
  CONTROL_NODE_META,
  CRYPTO_OPERATIONS,
  Condition,
  DEFAULT_PROJECT_ID,
  CATEGORY_PRESETS,
  Edge,
  HTTP_METHODS,
  HttpAuth,
  HttpAuthType,
  HttpOptions,
  KeyValue,
  MessageFormat,
  NodeConfig,
  NodeKind,
  NodePosition,
  OPERATORS,
  Operator,
  ParamSpec,
  Project,
  ScenarioDraft,
  ScenarioSet,
  ScenarioSummary,
  ScenarioVersionInfo,
  Step,
  TEST_CLASSES,
  TargetSpec,
  UNARY_OPERATORS,
  VAR_REF_RE,
  ValidationResult,
  categoryPreset,
  emptyDraft,
  nodeOutputPorts,
  portLabel,
} from "../scenario.models";

interface PaletteDrag {
  target?: string;
  action?: string;
  kind?: NodeKind;
}

/** HTTP config keys holding editable name/value rows. */
type HttpRowKey = "queryParameters" | "headers" | "bodyParameters";

/** A rendered output port on a node, positioned in world coordinates. */
interface RenderPort {
  port: string;
  label: string;
  cx: number;
  cy: number;
  connected: boolean;
}

/** Render model for one node on the canvas. */
interface CanvasNode {
  step: Step;
  kind: NodeKind;
  index: number;
  x: number;
  y: number;
  height: number;
  color: string;
  glyph: string;
  title: string;
  subtitle: string;
  inY: number;
  ports: RenderPort[];
  hasError: boolean;
  /** No incoming wire ⇒ a flow entry point (the worker starts at the first one). */
  isStart: boolean;
  /** No outgoing wire ⇒ a terminal/finish node. */
  isEnd: boolean;
}

/** A rendered wire between two node ports. */
interface RenderWire {
  d: string;
  index: number;
  label: string;
  midX: number;
  midY: number;
  invalid: boolean;
}

type DragState =
  | { kind: "node"; id: string; offX: number; offY: number; moved: boolean }
  | {
      kind: "pan";
      startX: number;
      startY: number;
      panX: number;
      panY: number;
      moved: boolean;
    }
  | { kind: "wire"; from: string; fromPort: string };

const NODE_W = 220;
const NODE_H = 84;
const PORT_GAP = 26;
const GRID_DOTS = 24;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 2;

/** A node in a starter flow: a catalog step + config overrides (may carry
 * `${ref.…}` references to earlier nodes in the same flow, rewritten on insert). */
interface FlowStep {
  ref: string;
  target: string;
  action: string;
  /** Overrides for top-level node config (crypto/http/init fields). */
  config?: Record<string, unknown>;
  /** Overrides for a code node's named Input rows (e.g. an ISO `message`). */
  inputs?: Record<string, string>;
  /** Assertions to set on the step (AI-assisted flows). */
  assertions?: { field: string; operator: string; expected?: unknown }[];
}

interface FlowTemplate {
  id: string;
  label: string;
  description: string;
  steps: FlowStep[];
}

/** One-click pre-wired flows inserted into the canvas. */
const FLOW_TEMPLATES: FlowTemplate[] = [
  {
    id: "emv_arqc_arpc",
    label: "EMV cryptogram (ARQC → ARPC)",
    description:
      "Issuer flow: build CDOL data, derive ICC master key + AC session key, " +
      "generate the ARQC, then generate and verify the ARPC.",
    steps: [
      { ref: "dol", target: "emv_tools", action: "dol_build" },
      {
        ref: "udk",
        target: "emv_crypto",
        action: "derive_icc_mk",
        config: {
          key: "0123456789ABCDEFFEDCBA9876543210",
          pan: "4111111111111111",
          psn: "00",
        },
      },
      {
        ref: "sk",
        target: "emv_crypto",
        action: "derive_session_key",
        config: { key: "${udk.response.udk}", atc: "001C" },
      },
      {
        ref: "arqc",
        target: "emv_crypto",
        action: "generate_arqc",
        config: {
          key: "${sk.response.session_key}",
          data: "${dol.response.data}",
        },
      },
      {
        ref: "arpc",
        target: "emv_crypto",
        action: "generate_arpc",
        config: {
          key: "${sk.response.session_key}",
          arqc: "${arqc.response.arqc}",
          arc: "3030",
          arpc_method: "1",
        },
      },
      {
        ref: "vfy",
        target: "emv_crypto",
        action: "verify_arpc",
        config: {
          key: "${sk.response.session_key}",
          arqc: "${arqc.response.arqc}",
          arc: "3030",
          arpc_method: "1",
          expected: "${arpc.response.arpc}",
        },
      },
    ],
  },
  {
    id: "iso8583_roundtrip",
    label: "ISO 8583 purchase (0200 → parse)",
    description:
      "Pack a 0200 financial request from field values, then parse the wire " +
      "message back — both using the selected Message Format.",
    steps: [
      { ref: "pack", target: "iso_messaging", action: "iso8583_pack" },
      {
        ref: "parse",
        target: "iso_messaging",
        action: "iso8583_parse",
        inputs: { message: "${pack.response.message}" },
      },
    ],
  },
  {
    id: "pin_block_roundtrip",
    label: "PIN block (encode → decode)",
    description:
      "Encode an ISO format-0 PIN block under a key, then decode it back to " +
      "the clear PIN with the same key and PAN.",
    steps: [
      {
        ref: "enc",
        target: "emv_crypto",
        action: "pin_block",
        config: {
          key: "0123456789ABCDEFFEDCBA9876543210",
          pin: "1234",
          pan: "4111111111111111",
        },
      },
      {
        ref: "dec",
        target: "emv_crypto",
        action: "pin_block_decode",
        config: {
          key: "0123456789ABCDEFFEDCBA9876543210",
          pin_block: "${enc.response.pin_block}",
          pan: "4111111111111111",
        },
      },
    ],
  },
];

@Component({
  selector: "app-constructor",
  standalone: true,
  imports: [
    CommonModule,
    DatePipe,
    FormsModule,
    RouterLink,
    CodeEditorComponent,
    RefAutocompleteDirective,
    IconComponent,
  ],
  templateUrl: "./constructor.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./constructor.component.scss",
})
export class ConstructorComponent implements OnInit {
  readonly theme = inject(ThemeService);

  private readonly api = inject(ScenarioApiService);
  private readonly connectionsStore = inject(ConnectionsService);
  private readonly groupsStore = inject(GroupsService);
  private readonly testData = inject(TestDataService);
  private readonly runApi = inject(RunApiService);
  private readonly runMonitor = inject(RunMonitorService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly ui = inject(UiService);
  private readonly participants = inject(ParticipantsService);

  /** Flow mode: the same editor authoring a participant flow instead of a
   * scenario. Load/save go to the participant-flows API; the palette gains
   * trigger/reply/state; scenario-only toolbar (run/debug/projects) is hidden. */
  flowMode = false;
  flowId: string | null = null;
  flowTrigger = "";
  /** Transient JSON edit buffers for reply/state nodes (keyed by node id), so
   * typing doesn't get reformatted on every keystroke; parsed at save time. */
  private flowJson: Record<string, string> = {};

  /** Set after a 409 so the next save overwrites instead of re-conflicting. */
  private overwriteOnSave = false;

  @HostListener("window:beforeunload", ["$event"])
  onBeforeUnload(event: BeforeUnloadEvent): void {
    if (this.dirty()) event.preventDefault();
  }

  @ViewChild("payloadArea")
  payloadArea?: ElementRef<HTMLTextAreaElement>;

  @ViewChild("canvasEl")
  canvasEl?: ElementRef<HTMLElement>;

  readonly operators = OPERATORS;
  readonly testClasses = TEST_CLASSES;
  readonly codeLanguages = CODE_LANGUAGES;
  readonly cryptoOperations = CRYPTO_OPERATIONS;
  readonly httpMethods = HTTP_METHODS;
  readonly authTypes: { value: HttpAuthType; label: string }[] = [
    { value: "none", label: "None" },
    { value: "bearer", label: "Bearer token" },
    { value: "basic", label: "Basic auth" },
    { value: "header", label: "Header / API key" },
    { value: "query", label: "Query param" },
  ];
  readonly nodeW = NODE_W;
  readonly nodeH = NODE_H;

  /** Control-flow node types offered in the palette. Flow kinds (trigger/reply/
   * state) only appear in flow mode; they're hidden from the scenario palette. */
  get controlPalette() {
    // Flow mode: offer flow kinds (trigger/reply/state) + generic logic, but
    // hide scenario-only kinds (call = sub-scenario, init = scenario seed).
    // Scenario mode: hide the flow-only kinds.
    const flowHidden: NodeKind[] = ["call", "init"];
    return (Object.keys(CONTROL_NODE_META) as Exclude<NodeKind, "action">[])
      .filter((k) =>
        this.flowMode ? !flowHidden.includes(k) : !FLOW_NODE_KINDS.includes(k),
      )
      .map((kind) => ({ kind, ...CONTROL_NODE_META[kind] }));
  }

  ancestorsCount(id: string): number {
    return this.ancestors(id).length;
  }

  /** Display label/glyph for a node in the detail-modal header. */
  nodeTitle(step: Step): string {
    if (step.kind && step.kind !== "action") {
      return CONTROL_NODE_META[step.kind].label;
    }
    return step.action || "Step";
  }

  nodeGlyph(step: Step): string {
    return step.kind && step.kind !== "action"
      ? CONTROL_NODE_META[step.kind].glyph
      : "";
  }

  // -- scenario state ------------------------------------------------------
  scenarioId: string | null = null;
  /** Target project for newly created scenarios (?project= query param). */
  private projectId: string | null = null;
  /** Set the new scenario joins on first save (?set= query param). */
  private setId: string | null = null;

  // -- organisation ----------------------------------------------------------
  readonly projects = signal<Project[]>([]);
  readonly projectSets = signal<ScenarioSet[]>([]);
  readonly currentProjectId = signal<string>(DEFAULT_PROJECT_ID);
  readonly version = signal<number | null>(null);
  readonly meta = signal<Omit<ScenarioDraft, "steps" | "edges" | "layout">>({
    name: "untitled_scenario",
    description: "",
    tags: [],
    stop_on_failure: true,
    timeout_ms: 10_000,
    test_class: "e2e",
  });
  readonly steps = signal<Step[]>([]);
  readonly edges = signal<Edge[]>([]);
  readonly layout = signal<Record<string, NodePosition>>({});
  /** Scenario-scoped variables, edited as rows; referenced as ${vars.NAME}. */
  readonly varRows = signal<{ key: string; value: string; secret?: boolean }[]>(
    [],
  );
  readonly dirty = signal(false);

  // -- editor state --------------------------------------------------------
  readonly catalog = signal<TargetSpec[]>([]);
  /** Registered Message Formats, for the ISO step format picker. */
  readonly messageFormats = signal<MessageFormat[]>([]);
  /** Other scenarios, for the Call Scenario node picker. */
  readonly scenarioList = signal<ScenarioSummary[]>([]);
  /** Sub-scenario choices for the call picker (excludes the current scenario). */
  readonly callableScenarios = computed(() =>
    this.scenarioList().filter((s) => s.id !== this.scenarioId),
  );
  readonly selectedId = signal<string | null>(null);
  readonly payloadText = signal("");
  readonly payloadError = signal<string | null>(null);
  readonly validation = signal<ValidationResult | null>(null);
  readonly versions = signal<ScenarioVersionInfo[] | null>(null);
  readonly showHistory = signal(false);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly saveComment = signal("");
  tagsText = "";

  // -- canvas (n8n-style board) --------------------------------------------
  readonly zoom = signal(1);
  readonly pan = signal({ x: 0, y: 0 });
  readonly wireDrag = signal<{
    from: string;
    fromPort: string;
    x: number;
    y: number;
  } | null>(null);
  private drag: DragState | null = null;

  readonly nodes = computed<CanvasNode[]>(() => {
    const layout = this.layout();
    const edges = this.edges();
    const hasIncoming = new Set(edges.map((e) => e.target));
    const hasOutgoing = new Set(edges.map((e) => e.source));
    return this.steps().map((s, i) => {
      const pos = layout[s.id] ?? autoPos(i);
      const kind = s.kind ?? "action";
      const portIds = nodeOutputPorts(s);
      const height = this.nodeHeight(s);
      const ports: RenderPort[] = portIds.map((port, pi) => ({
        port,
        label: portLabel(s, port),
        cx: pos.x + NODE_W,
        cy: pos.y + ((pi + 1) * height) / (portIds.length + 1),
        connected: edges.some(
          (e) => e.source === s.id && e.source_port === port,
        ),
      }));
      const meta = kind === "action" ? null : CONTROL_NODE_META[kind];
      return {
        step: s,
        kind,
        index: i + 1,
        x: pos.x,
        y: pos.y,
        height,
        color: meta?.color ?? this.targetColor(s.target),
        glyph: meta?.glyph ?? "",
        title:
          kind === "action" ? s.action : (s.config?.stepLabel ?? meta!.label),
        subtitle:
          kind === "action"
            ? s.config?.connection
              ? `${this.targetLabel(s.target)} · ⇄ ${s.config.connection}`
              : this.targetLabel(s.target)
            : this.controlSummary(s),
        inY: pos.y + height / 2,
        ports,
        hasError: this.stepHasRefError(s, i),
        isStart: !hasIncoming.has(s.id),
        isEnd: !hasOutgoing.has(s.id),
      };
    });
  });

  /**
   * All `${...}` references available for autocomplete: each step's response
   * fields (from the catalog) plus `vars.*` and `tables.*`.
   */
  readonly allRefs = computed<string[]>(() => {
    const cat = this.catalog();
    const refs: string[] = [];
    for (const s of this.steps()) {
      const kind = s.kind ?? "action";
      if (kind === "action") {
        const spec = cat.find((t) => t.target === s.target);
        const action = spec?.actions.find((a) => a.name === s.action);
        const fields = action?.response_fields ?? [];
        if (fields.length) {
          for (const f of fields) refs.push(`\${${s.id}.response.${f}}`);
        } else {
          refs.push(`\${${s.id}.response}`);
        }
      } else if (kind === "http") {
        for (const f of ["status_code", "headers", "body"]) {
          refs.push(`\${${s.id}.response.${f}}`);
        }
      } else if (
        kind !== "if" &&
        kind !== "switch" &&
        kind !== "merge" &&
        kind !== "wait"
      ) {
        // code / init / loop produce data
        refs.push(`\${${s.id}.response}`);
      }
    }
    for (const r of this.varRows()) {
      if (r.key.trim()) refs.push(`\${vars.${r.key.trim()}}`);
    }
    for (const name of Object.keys(this.tablesRuntime())) {
      refs.push(`\${tables.${name}}`);
    }
    return refs;
  });

  /** Step id of the actual entry node (first with no incoming wire). */
  readonly entryId = computed<string | null>(() => {
    const n = this.nodes().find((x) => x.isStart);
    return n ? n.step.id : (this.nodes()[0]?.step.id ?? null);
  });

  /** A node lookup for wire geometry. */
  private nodeById(id: string): CanvasNode | undefined {
    return this.nodes().find((n) => n.step.id === id);
  }

  /** Bezier paths for every explicit edge, with branch labels. */
  readonly wires = computed<RenderWire[]>(() => {
    const nodes = this.nodes();
    const byId = new Map(nodes.map((n) => [n.step.id, n]));
    const out: RenderWire[] = [];
    this.edges().forEach((e, index) => {
      const from = byId.get(e.source);
      const to = byId.get(e.target);
      if (!from || !to) return;
      const port = from.ports.find((p) => p.port === e.source_port);
      const sx = port?.cx ?? from.x + NODE_W;
      const sy = port?.cy ?? from.y + from.height / 2;
      const label =
        from.kind === "action" || e.source_port === "out"
          ? ""
          : (port?.label ?? e.source_port);
      out.push({
        d: bezier(sx, sy, to.x, to.inY),
        index,
        label,
        midX: (sx + to.x) / 2,
        midY: (sy + to.inY) / 2,
        invalid: this.connectionError(e.source, e.target) !== null,
      });
    });
    return out;
  });

  /** Dashed wire that follows the cursor while connecting. */
  readonly tempWirePath = computed<string | null>(() => {
    const drag = this.wireDrag();
    if (!drag) return null;
    const from = this.nodeById(drag.from);
    if (!from) return null;
    const port = from.ports.find((p) => p.port === drag.fromPort);
    const sx = port?.cx ?? from.x + NODE_W;
    const sy = port?.cy ?? from.y + from.height / 2;
    return bezier(sx, sy, drag.x, drag.y);
  });

  readonly worldTransform = computed(
    () =>
      `translate(${this.pan().x}px, ${this.pan().y}px) scale(${this.zoom()})`,
  );
  readonly gridSize = computed(() => `${GRID_DOTS * this.zoom()}px`);
  readonly gridPos = computed(() => `${this.pan().x}px ${this.pan().y}px`);

  readonly selectedStep = computed(
    () => this.steps().find((s) => s.id === this.selectedId()) ?? null,
  );

  readonly selectedIndex = computed(() =>
    this.steps().findIndex((s) => s.id === this.selectedId()),
  );

  /** Catalog spec for the selected step's target. */
  readonly selectedTargetSpec = computed<TargetSpec | null>(() => {
    const step = this.selectedStep();
    if (!step) return null;
    return this.catalog().find((t) => t.target === step.target) ?? null;
  });

  /**
   * Variable references the selected step may use:
   * response fields of every step that runs before it.
   */
  readonly availableRefs = computed<{ step: string; ref: string }[]>(() => {
    const idx = this.selectedIndex();
    if (idx < 0) return [];
    const refs: { step: string; ref: string }[] = [];
    for (const earlier of this.steps().slice(0, idx)) {
      const spec = this.catalog().find((t) => t.target === earlier.target);
      const action = spec?.actions.find((a) => a.name === earlier.action);
      for (const field of action?.response_fields ?? []) {
        refs.push({
          step: earlier.id,
          ref: `\${${earlier.id}.response.${field}}`,
        });
      }
    }
    return refs;
  });

  /** When true, the action payload editor shows the raw JSON textarea. */
  readonly rawPayload = signal(false);

  /** Catalog action spec for the selected step. */
  readonly selectedAction = computed<ActionSpec | null>(() => {
    const step = this.selectedStep();
    const spec = this.selectedTargetSpec();
    if (!step || !spec) return null;
    return spec.actions.find((a) => a.name === step.action) ?? null;
  });

  /**
   * Typed parameters to render as a structured form for the selected action.
   * Prefers the catalog's explicit ``params``; otherwise derives string params
   * from ``payload_hint`` keys so older catalog entries still get a form.
   */
  readonly selectedParams = computed<ParamSpec[]>(() => {
    const action = this.selectedAction();
    if (!action) return [];
    if (action.params?.length) return action.params;
    return Object.keys(action.payload_hint ?? {}).map((name) => ({
      name,
      type: "string" as const,
    }));
  });

  /** Response fields of the selected action — used to make `field` selectable. */
  readonly selectedResponseFields = computed<string[]>(
    () => this.selectedAction()?.response_fields ?? [],
  );

  /** A step's payload as an object. A payload can also be a whole-string
   * reference (a flow reply node echoing e.g. `"${fwd.response}"`) — the typed
   * param editors below only apply to object payloads, so a string reads as {}. */
  private payloadObj(step: Step | undefined | null): Record<string, unknown> {
    const p = step?.payload;
    return p && typeof p === "object" ? p : {};
  }

  /** Current value of a param as a string for text/number/enum inputs. */
  paramStr(name: string): string {
    const v = this.payloadObj(this.selectedStep())[name];
    if (v === undefined || v === null) return "";
    return typeof v === "object" ? JSON.stringify(v) : String(v);
  }

  /** Current value of a boolean param. */
  paramBool(name: string): boolean {
    return this.payloadObj(this.selectedStep())[name] === true;
  }

  /** True when an enum value is set but not one of the offered options. */
  enumIsCustom(p: ParamSpec): boolean {
    const v = this.paramStr(p.name);
    return v !== "" && !this.effectiveOptions(p).includes(v);
  }

  /** Message Formats offered by a "format" (dialect) param, filtered by protocol. */
  dialectFormats(p: ParamSpec): MessageFormat[] {
    return this.formatsFor(p.protocol || "iso8583");
  }

  /** The dialect (Message Format) currently bound on the selected step, if any —
   *  read from the step's "format"-typed param value. */
  selectedDialect(): MessageFormat | undefined {
    const fp = this.selectedParams().find((p) => p.type === "format");
    const id = fp ? this.payloadObj(this.selectedStep())[fp.name] : undefined;
    return id ? this.messageFormats().find((f) => f.id === id) : undefined;
  }

  /** Options for an enum param. When it's flagged `options_from_format` and a
   *  dialect is bound, its MTI list (definition.mti) wins — so the MTI menu
   *  follows the chosen dialect (1987 → 0xxx, 1993 → 1xxx). */
  effectiveOptions(p: ParamSpec): string[] {
    if (p.options_from_format) {
      const def = this.selectedDialect()?.definition as
        | Record<string, unknown>
        | undefined;
      const mtis = def?.["mti"] as string[] | undefined;
      if (mtis?.length) return mtis;
    }
    return p.options ?? [];
  }

  /** Bind a dialect: snapshot its DE table into the step's `fields`, record the
   *  id on the param, and — if the current MTI isn't valid for the new dialect —
   *  switch to the dialect's first MTI. Clearing it drops both. */
  applyDialect(p: ParamSpec, formatId: string): void {
    const step = this.selectedStep();
    if (!step) return;
    const payload: Record<string, unknown> = { ...this.payloadObj(step) };
    if (!formatId) {
      delete payload[p.name];
      delete payload["fields"];
    } else {
      const fmt = this.messageFormats().find((f) => f.id === formatId);
      if (!fmt) return;
      const def = (fmt.definition ?? {}) as Record<string, unknown>;
      payload[p.name] = formatId;
      payload["fields"] = def["fields"] ?? {};
      const mtis = (def["mti"] as string[] | undefined) ?? [];
      if (mtis.length && !mtis.includes(String(payload["mti"] ?? ""))) {
        payload["mti"] = mtis[0];
      }
    }
    this.updateStep(step.id, { payload });
    this.payloadText.set(JSON.stringify(payload, null, 2));
    this.payloadError.set(null);
  }

  /** Write one structured param into the selected step's payload (typed). */
  setParam(p: ParamSpec, raw: unknown): void {
    const step = this.selectedStep();
    if (!step) return;
    let value: unknown = raw;
    const text = typeof raw === "string" ? raw : String(raw);
    const isRef = typeof raw === "string" && raw.includes("${");
    if (p.type === "number" && !isRef) {
      if (text.trim() === "") value = "";
      else {
        const n = Number(text);
        value = Number.isNaN(n) ? text : n;
      }
    } else if (p.type === "boolean") {
      value = raw === true || raw === "true";
    } else if (p.type === "json" && !isRef) {
      try {
        value = JSON.parse(text);
      } catch {
        value = text;
      }
    } else {
      value = raw;
    }
    const payload: Record<string, unknown> = { ...this.payloadObj(step) };
    if (value === "" && !p.required) delete payload[p.name];
    else payload[p.name] = value;
    this.updateStep(step.id, { payload });
    this.payloadText.set(JSON.stringify(payload, null, 2));
    this.payloadError.set(null);
  }

  ngOnInit(): void {
    this.api.catalog().subscribe({
      next: (cat) => this.catalog.set(cat),
      error: () =>
        this.error.set(
          "Could not reach the scenario service — start it with " +
            "uvicorn api.main:app --port 8000 (in packages/scenario-service).",
        ),
    });

    this.api.formats().subscribe({
      next: (list) => this.messageFormats.set(list),
      error: () => this.messageFormats.set([]),
    });
    this.api.list().subscribe({
      next: (list) => this.scenarioList.set(list),
      error: () => this.scenarioList.set([]),
    });
    // Starter flows come from the store; keep the built-in seed on error.
    this.api.starterFlows().subscribe({
      next: (list) => {
        if (list.length) this.flowTemplates.set(list);
      },
      error: () => {},
    });
    // Saved connections, for the per-step Connection picker.
    this.connectionsStore.reload();
    // Participant groups (fleets) — offered as outbound targets in flow mode.
    this.groupsStore.reload();
    // Saved test-data pools, for the scenario-level pool pickers.
    this.testData.reloadCardPools();
    this.testData.reloadTerminalPools();

    this.loadEnvironments();
    this.projectId = this.route.snapshot.queryParamMap.get("project");
    this.setId = this.route.snapshot.queryParamMap.get("set");
    this.currentProjectId.set(this.projectId ?? DEFAULT_PROJECT_ID);
    this.api.projects().subscribe({
      next: (list) => this.projects.set(list),
      error: () => this.projects.set([]),
    });
    const id = this.route.snapshot.paramMap.get("id");
    this.flowMode = this.route.snapshot.data["mode"] === "flow";
    if (this.flowMode) {
      if (id && id !== "new") this.loadFlow(id);
      else this.applyDraft(emptyDraft(), null);
      return;
    }
    if (id) {
      this.scenarioId = id;
      this.load(id);
    } else {
      this.applyDraft(emptyDraft(), null);
      this.loadProjectSets();
    }
  }

  // -- flow mode (reuse this editor to author a participant flow) -----------

  private loadFlow(id: string): void {
    this.flowId = id;
    this.busy.set(true);
    this.participants.get(id).subscribe({
      next: (f: any) => {
        const layout: Record<string, NodePosition> = {};
        const steps = (f.nodes ?? []).map((n: any) => {
          const cfg = { ...(n.config ?? {}) };
          const pos = cfg["_pos"];
          if (pos) layout[n.id] = { x: pos.x, y: pos.y };
          delete cfg["_pos"];
          return { ...n, config: cfg };
        });
        this.applyDraft(
          {
            ...emptyDraft(),
            name: f.name ?? "",
            steps,
            edges: f.edges ?? [],
            layout,
          },
          null,
        );
        this.flowTrigger = f.trigger?.connection ?? "";
        this.busy.set(false);
      },
      error: () => {
        this.busy.set(false);
        this.error.set("Could not load participant flow.");
      },
    });
  }

  /** JSON editor buffer for a reply node's response action / a state node's ops.
   * reply edits ``payload``; state edits ``config`` (set/incr/append). */
  flowNodeJson(step: Step): string {
    if (this.flowJson[step.id] === undefined) {
      const cfg = (step.config ?? {}) as Record<string, unknown>;
      const obj =
        step.kind === "reply"
          ? (step.payload ?? {})
          : {
              set: cfg["set"] ?? {},
              incr: cfg["incr"] ?? {},
              append: cfg["append"] ?? {},
            };
      this.flowJson[step.id] = JSON.stringify(obj, null, 2);
    }
    return this.flowJson[step.id];
  }

  setFlowNodeJson(step: Step, text: string): void {
    this.flowJson[step.id] = text;
    this.dirty.set(true);
  }

  /** Add an outbound "call a downstream connection" node to a flow. The target
   * picks the adapter family for the Connection picker (ISO 8583 TCP, header-echo
   * HSM, or gRPC); the orchestrator repoints the node onto the chosen connection
   * at launch, so the connection's own adapter is what actually runs. */
  addFlowCall(
    target: "tcp_iso8583" | "hsm" | "grpc" | "http" = "tcp_iso8583",
  ): void {
    const action =
      target === "grpc" || target === "http"
        ? "call"
        : target === "hsm"
          ? "diagnostic"
          : "send_0200";
    this.addStep(target, action);
  }

  /** Saved inbound (listening) connections — candidates for a flow's trigger. */
  get inboundConnections() {
    return this.connections().filter((c) => c.mode === "inbound");
  }

  /** The participant-flow document as currently drafted in the editor (reply/
   * state JSON buffers parsed in). Returns null (with a toast) on bad JSON —
   * shared by Save and the flow debug run, so both see identical state. */
  private buildFlowDoc():
    | (Partial<ParticipantFlow> & {
        nodes: unknown[];
        edges: unknown[];
      })
    | null {
    const draft = this.buildDraft();
    let badJson = false;
    const nodes = draft.steps.map((s) => {
      let payload = s.payload;
      let config: Record<string, unknown> = { ...(s.config ?? {}) };
      const buf = this.flowJson[s.id];
      if (buf !== undefined && (s.kind === "reply" || s.kind === "state")) {
        try {
          const parsed = JSON.parse(buf || "{}");
          if (s.kind === "reply") payload = parsed;
          else config = { ...config, ...parsed };
        } catch {
          badJson = true;
        }
      }
      return {
        ...s,
        payload,
        config: { ...config, _pos: draft.layout?.[s.id] ?? { x: 0, y: 0 } },
      };
    });
    if (badJson) {
      this.ui.toast(
        "error",
        "A reply/state node has invalid JSON — fix it first.",
      );
      return null;
    }
    return {
      name: draft.name.trim(),
      description: (draft as any).description ?? "",
      trigger: { connection: this.flowTrigger.trim() },
      nodes,
      edges: draft.edges ?? [],
      variables: {},
    };
  }

  private saveFlow(): void {
    if (!this.meta().name?.trim()) {
      this.ui.toast("error", "Give the flow a name.");
      return;
    }
    const body = this.buildFlowDoc();
    if (!body) return;
    this.busy.set(true);
    const op = this.flowId
      ? this.participants.save(this.flowId, body)
      : this.participants.create(body);
    op.subscribe({
      next: (f) => {
        this.busy.set(false);
        this.dirty.set(false);
        this.flowId = f.id;
        this.ui.toast("success", `Saved flow “${f.name}”`);
      },
      error: (e) => {
        this.busy.set(false);
        this.ui.toast("error", this.httpErrorText(e) || "Save failed.");
      },
    });
  }

  /** Human-readable text from an HTTP error. FastAPI sends a string ``detail``
   *  for HTTPExceptions but an ARRAY of ``{loc,msg}`` for Pydantic 422s — the
   *  latter used to render as "[object Object]". */
  private httpErrorText(e: unknown): string {
    const detail = (e as { error?: { detail?: unknown } })?.error?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => {
          const loc = Array.isArray(d?.loc) ? d.loc.join(".") : "";
          return [loc, d?.msg].filter(Boolean).join(": ");
        })
        .filter(Boolean)
        .join("; ");
    }
    const msg =
      (e as { error?: { message?: string }; message?: string })?.error
        ?.message ?? (e as { message?: string })?.message;
    return typeof msg === "string" ? msg : "";
  }

  // -- load / save / versions ---------------------------------------------

  private load(id: string, version?: number): void {
    this.busy.set(true);
    this.api.get(id, version).subscribe({
      next: (sc) => {
        this.applyDraft(sc, sc.version);
        this.currentProjectId.set(sc.project_id);
        this.loadProjectSets();
        this.busy.set(false);
        this.dirty.set(version !== undefined);
      },
      error: () => {
        this.busy.set(false);
        this.error.set(`Failed to load scenario '${id}'.`);
      },
    });
  }

  private loadProjectSets(): void {
    this.api.sets(this.currentProjectId()).subscribe({
      next: (sets) => this.projectSets.set(sets),
      error: () => this.projectSets.set([]),
    });
  }

  // -- project & set membership ---------------------------------------------

  async onProjectChange(newProjectId: string): Promise<void> {
    const previous = this.currentProjectId();
    if (newProjectId === previous) return;
    if (!this.scenarioId) {
      // Not saved yet — just retarget the upcoming create.
      this.projectId = newProjectId;
      this.setId = null; // old set belongs to the old project
      this.currentProjectId.set(newProjectId);
      this.loadProjectSets();
      return;
    }
    const name =
      this.projects().find((p) => p.id === newProjectId)?.name ?? newProjectId;
    const ok = await this.ui.confirm(
      `Move this test case to project "${name}"? ` +
        "Its memberships in the current project's sets will be removed.",
      { title: "Move to project", confirmLabel: "Move" },
    );
    if (!ok) {
      this.currentProjectId.set(previous); // revert the select
      return;
    }
    this.api.move(this.scenarioId, newProjectId).subscribe({
      next: (sc) => {
        this.currentProjectId.set(sc.project_id);
        this.loadProjectSets();
        this.ui.toast("success", `Moved to "${name}"`);
      },
      error: () => {
        this.currentProjectId.set(previous);
        this.ui.toast("error", "Failed to move the test case.");
      },
    });
  }

  inSet(set: ScenarioSet): boolean {
    return (
      this.scenarioId !== null && set.scenario_ids.includes(this.scenarioId)
    );
  }

  toggleSet(set: ScenarioSet): void {
    if (!this.scenarioId) return;
    const req = this.inSet(set)
      ? this.api.removeFromSet(set.id, this.scenarioId)
      : this.api.addToSet(set.id, this.scenarioId);
    req.subscribe({
      next: (updated) =>
        this.projectSets.update((list) =>
          list.map((s) => (s.id === updated.id ? updated : s)),
        ),
      error: () => this.ui.toast("error", "Failed to update set membership."),
    });
  }

  private applyDraft(draft: ScenarioDraft, version: number | null): void {
    const { steps, edges, layout, variables, secret_vars, ...meta } = draft;
    this.meta.set(meta);
    this.tagsText = meta.tags.join(", ");
    const secrets = new Set(secret_vars ?? []);
    this.varRows.set(
      Object.entries(variables ?? {}).map(([key, v]) => ({
        key,
        value: typeof v === "string" ? v : JSON.stringify(v),
        secret: secrets.has(key),
      })),
    );
    const clonedSteps = steps.map((s) => structuredClone(s));
    this.steps.set(clonedSteps);
    // Legacy scenarios carry no edges — synthesise a sequential chain so the
    // wires render and the flow stays editable as a graph.
    this.edges.set(
      edges && edges.length
        ? edges.map((e) => ({ ...e }))
        : sequentialEdges(clonedSteps),
    );
    this.layout.set({ ...(layout ?? {}) });
    this.version.set(version);
    this.selectedId.set(null);
    this.validation.set(null);
    setTimeout(() => this.fitView());
  }

  buildDraft(): ScenarioDraft {
    // Materialise auto-positions so the saved layout is complete.
    const layout: Record<string, NodePosition> = {};
    for (const n of this.nodes()) layout[n.step.id] = { x: n.x, y: n.y };
    const variables: Record<string, unknown> = {};
    const secret_vars: string[] = [];
    for (const r of this.varRows()) {
      const k = r.key.trim();
      if (!k) continue;
      variables[k] = this.parseScalar(r.value);
      if (r.secret) secret_vars.push(k);
    }
    return {
      ...this.meta(),
      steps: this.steps(),
      variables,
      secret_vars,
      edges: this.edges(),
      layout,
    };
  }

  toggleVarSecret(i: number): void {
    this.varRows.update((rows) =>
      rows.map((r, j) => (j === i ? { ...r, secret: !r.secret } : r)),
    );
    this.dirty.set(true);
  }

  addVar(): void {
    this.varRows.update((rows) => [...rows, { key: "", value: "" }]);
    this.dirty.set(true);
  }

  removeVar(i: number): void {
    this.varRows.update((rows) => rows.filter((_, j) => j !== i));
    this.dirty.set(true);
  }

  updateVar(i: number, patch: Partial<{ key: string; value: string }>): void {
    this.varRows.update((rows) =>
      rows.map((r, j) => (j === i ? { ...r, ...patch } : r)),
    );
    this.dirty.set(true);
  }

  private memberSetIds(): string[] {
    return this.projectSets()
      .filter((s) => this.inSet(s))
      .map((s) => s.id);
  }

  save(): void {
    if (this.flowMode) {
      this.saveFlow();
      return;
    }
    const draft = this.buildDraft();
    const comment = this.saveComment();
    this.busy.set(true);
    this.error.set(null);
    const baseVersion = this.overwriteOnSave ? null : this.version();
    const req = this.scenarioId
      ? this.api.update(this.scenarioId, draft, comment, baseVersion)
      : this.api.create(draft, comment, this.projectId);
    req.subscribe({
      next: (sc) => {
        this.busy.set(false);
        this.dirty.set(false);
        this.overwriteOnSave = false;
        this.version.set(sc.version);
        this.saveComment.set("");
        this.versions.set(null);
        this.ui.toast("success", `Saved ${sc.name} as v${sc.version}`);
        if (!this.scenarioId) {
          this.scenarioId = sc.id;
          this.currentProjectId.set(sc.project_id);
          if (this.setId) {
            const setId = this.setId;
            this.setId = null; // join the set once, on first save
            this.api.addToSet(setId, sc.id).subscribe({
              next: (set) => {
                this.ui.toast("success", `Added to set "${set.name}"`);
                this.loadProjectSets();
              },
              error: () =>
                this.ui.toast(
                  "error",
                  "Saved, but could not add the case to the set.",
                ),
            });
          } else {
            this.loadProjectSets();
          }
          this.router.navigate(["/constructor", sc.id], { replaceUrl: true });
        }
      },
      error: (err) => {
        this.busy.set(false);
        if (err?.status === 409) {
          const current = err?.error?.detail?.current_version;
          this.overwriteOnSave = true;
          this.error.set(
            `Save conflict — someone else saved v${current} while you were ` +
              "editing. Check History to review their change, or hit Save " +
              "again to overwrite it with yours.",
          );
          this.ui.toast(
            "error",
            "Version conflict — scenario changed remotely.",
          );
          return;
        }
        const issues = err?.error?.detail;
        if (Array.isArray(issues)) {
          this.validation.set({ valid: false, issues });
          this.error.set("Save rejected — fix the validation errors below.");
        } else {
          this.error.set("Save failed — is the scenario service running?");
          this.ui.toast("error", "Save failed.");
        }
      },
    });
  }

  /** Download the last saved version as a portable JSON or YAML file. */
  exportAs(format: "json" | "yaml"): void {
    if (!this.scenarioId) return;
    if (this.dirty()) {
      this.ui.toast(
        "error",
        "Unsaved changes are not exported — save first to include them.",
      );
    }
    this.api.export(this.scenarioId, format).subscribe({
      next: (blob) => {
        const name =
          this.meta().name.replace(/[^A-Za-z0-9._-]+/g, "_") || "scenario";
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${name}.${format}`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: () => this.ui.toast("error", "Export failed."),
    });
  }

  validateNow(): void {
    this.api.validate(this.buildDraft()).subscribe({
      next: (result) => this.validation.set(result),
      error: () => this.error.set("Validation request failed."),
    });
  }

  toggleHistory(): void {
    this.showHistory.update((v) => !v);
    if (this.showHistory() && this.scenarioId && this.versions() === null) {
      this.api.versions(this.scenarioId).subscribe({
        next: (vs) => this.versions.set(vs),
        error: () => this.error.set("Failed to load version history."),
      });
    }
  }

  viewVersion(v: ScenarioVersionInfo): void {
    if (!this.scenarioId) return;
    this.load(this.scenarioId, v.version);
    this.showHistory.set(false);
  }

  async restoreVersion(v: ScenarioVersionInfo, event: Event): Promise<void> {
    event.stopPropagation();
    if (!this.scenarioId) return;
    const ok = await this.ui.confirm(
      `Restore v${v.version}? It will be copied forward as a new version — nothing is lost.`,
      { title: "Restore version", confirmLabel: `Restore v${v.version}` },
    );
    if (!ok) return;
    this.api.restore(this.scenarioId, v.version).subscribe({
      next: (sc) => {
        this.applyDraft(sc, sc.version);
        this.versions.set(null);
        this.showHistory.set(false);
        this.ui.toast("success", `Restored v${v.version} as v${sc.version}`);
      },
      error: () => this.ui.toast("error", "Restore failed."),
    });
  }

  // -- meta ----------------------------------------------------------------

  readonly categoryPresets = CATEGORY_PRESETS;

  updateMeta<K extends keyof Omit<ScenarioDraft, "steps" | "edges" | "layout">>(
    key: K,
    value: Omit<ScenarioDraft, "steps" | "edges" | "layout">[K],
  ): void {
    this.meta.update((m) => ({ ...m, [key]: value }));
    this.dirty.set(true);
  }

  /** Pick a category — stamps its matching logo (icon) and accent color. */
  setCategory(label: string): void {
    const preset = categoryPreset(label);
    this.meta.update((m) => ({
      ...m,
      category: label,
      color: preset?.color ?? "",
      icon: preset?.icon ?? "",
    }));
    this.dirty.set(true);
  }

  /** The logo + color for the currently-selected category (settings preview). */
  metaBadge(): { icon: string; color: string } | null {
    const m = this.meta();
    if (m.icon || m.color) {
      return { icon: m.icon ?? "layers", color: m.color || "#8b949e" };
    }
    const preset = categoryPreset(m.category);
    return preset ? { icon: preset.icon, color: preset.color } : null;
  }

  onTagsChange(text: string): void {
    this.tagsText = text;
    this.updateMeta(
      "tags",
      text
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
    );
  }

  // -- palette / canvas ----------------------------------------------------

  onPaletteDragStart(
    event: DragEvent,
    target: TargetSpec,
    action: ActionSpec,
  ): void {
    event.dataTransfer?.setData(
      "application/json",
      JSON.stringify({
        target: target.target,
        action: action.name,
      } satisfies PaletteDrag),
    );
  }

  onCanvasDragOver(event: DragEvent): void {
    event.preventDefault();
  }

  onControlDragStart(event: DragEvent, kind: NodeKind): void {
    event.dataTransfer?.setData(
      "application/json",
      JSON.stringify({ kind } satisfies PaletteDrag),
    );
  }

  onCanvasDrop(event: DragEvent): void {
    event.preventDefault();
    const raw = event.dataTransfer?.getData("application/json");
    if (!raw) return;
    const drag = JSON.parse(raw) as PaletteDrag;
    const w = this.toWorld(event.clientX, event.clientY);
    const pos = {
      x: snap(w.x - NODE_W / 2),
      y: snap(w.y - NODE_H / 2),
    };
    if (drag.kind && drag.kind !== "action") {
      this.addControlNode(drag.kind, pos);
    } else if (drag.target && drag.action) {
      this.addCatalogStep(drag.target, drag.action, pos);
    }
  }

  /** The node a freshly added node should auto-connect from, if any. */
  private autoConnectFrom(): CanvasNode | undefined {
    const nodes = this.nodes();
    // prefer the selected node, else the last node in the list
    const selected = this.selectedId();
    return nodes.find((n) => n.step.id === selected) ?? nodes[nodes.length - 1];
  }

  /** Wire a new node in from the first free output port of `from`. */
  private autoConnect(from: CanvasNode | undefined, toId: string): void {
    if (!from) return;
    const free = from.ports.find((p) => !p.connected);
    if (free) this.connect(from.step.id, free.port, toId);
  }

  private defaultPosition(pos?: NodePosition): NodePosition {
    if (pos) return pos;
    const nodes = this.nodes();
    const last = nodes[nodes.length - 1];
    return last
      ? { x: last.x + AUTO_GAP, y: last.y }
      : { x: AUTO_X, y: AUTO_Y };
  }

  /**
   * Add a step from the catalog. Adapter actions become `action` nodes; custom
   * actions backed by HTTP/code materialise the matching node from their
   * template, so a user-defined step runs without a backend adapter.
   */
  /** One-click pre-wired starter flows shown in the palette. Seeded with the
   * built-ins for an instant palette, then refreshed from the store (so flows
   * added/edited in Manage → Starter Flows show up here). */
  readonly flowTemplates = signal<FlowTemplate[]>(FLOW_TEMPLATES);

  /**
   * Insert a starter flow: materialise each catalog step in order (auto-wired
   * into a chain) and apply its config overrides, rewriting `${ref.…}` links to
   * the real generated node ids.
   */
  // -- AI assistant ---------------------------------------------------------

  aiPrompt = "";
  readonly aiBusy = signal(false);
  /** A pending AI proposal awaiting the user's Apply / Discard. */
  readonly aiProposal = signal<AssistResult | null>(null);

  /** Ask the assistant for a flow; show it as a preview (not inserted yet). */
  askAi(): void {
    const prompt = this.aiPrompt.trim();
    if (!prompt) return;
    this.aiBusy.set(true);
    this.aiProposal.set(null);
    const context = this.steps()
      .filter((s) => (s.kind ?? "action") === "action")
      .map((s) => ({ id: s.id, target: s.target, action: s.action }));
    this.api.assistScenario(prompt, context).subscribe({
      next: (res) => {
        this.aiBusy.set(false);
        if (!res.flow.steps?.length) {
          this.ui.toast(
            "error",
            res.notes?.[0] ?? "Couldn't map that to known steps.",
          );
          return;
        }
        this.aiProposal.set(res); // preview — user applies or discards
      },
      error: () => {
        this.aiBusy.set(false);
        this.ui.toast(
          "error",
          "Assist failed — is the scenario service running?",
        );
      },
    });
  }

  /** Insert the previewed proposal onto the canvas. */
  applyProposal(): void {
    const res = this.aiProposal();
    if (!res) return;
    this.insertFlow(res.flow as unknown as FlowTemplate);
    this.aiProposal.set(null);
    this.aiPrompt = "";
    const verb = res.mode === "extend" ? "Added" : "Inserted";
    this.ui.toast(
      "success",
      `${verb} ${res.flow.steps.length} step(s) — review before running.`,
    );
  }

  discardProposal(): void {
    this.aiProposal.set(null);
  }

  /** One-line summary of a proposed step's assertions (for the preview). */
  proposalAssertions(step: {
    assertions?: { field: string; operator: string; expected?: unknown }[];
  }): string {
    return (step.assertions ?? [])
      .map(
        (a) =>
          `${a.field} ${a.operator}${a.expected !== undefined && a.expected !== null ? " " + a.expected : ""}`,
      )
      .join(", ");
  }

  insertFlow(template: FlowTemplate): void {
    if (!this.catalog().length) return;
    const refMap: Record<string, string> = {};
    for (const fs of template.steps) {
      this.addCatalogStep(fs.target, fs.action);
      const newId = this.selectedId();
      if (!newId) continue;
      refMap[fs.ref] = newId;
      const step = this.steps().find((s) => s.id === newId);
      if (!step) continue;
      if (fs.config) {
        const patch: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(fs.config)) {
          patch[k] = typeof v === "string" ? rewriteFlowRefs(v, refMap) : v;
        }
        this.updateConfig(step, patch as Partial<NodeConfig>);
      }
      if (fs.inputs) {
        const fresh = this.steps().find((s) => s.id === newId);
        const rows: KeyValue[] = [...(fresh?.config?.inputs ?? [])];
        for (const [name, raw] of Object.entries(fs.inputs)) {
          const value = rewriteFlowRefs(raw, refMap);
          const i = rows.findIndex((r) => r.name === name);
          if (i >= 0) rows[i] = { ...rows[i], value };
          else rows.push({ name, value });
        }
        this.updateConfig(this.steps().find((s) => s.id === newId)!, {
          inputs: rows,
        });
      }
      if (fs.assertions?.length) {
        const asserts = fs.assertions.map((a) => ({
          field: a.field,
          operator: a.operator as Assertion["operator"],
          expected:
            typeof a.expected === "string"
              ? rewriteFlowRefs(a.expected, refMap)
              : a.expected,
        })) as Assertion[];
        this.updateStep(newId, { assertions: asserts });
      }
    }
    this.dirty.set(true);
  }

  addCatalogStep(target: string, action: string, pos?: NodePosition): void {
    const spec = this.catalog().find((t) => t.target === target);
    const actionSpec = spec?.actions.find((a) => a.name === action);
    const behavior = actionSpec?.behavior;
    if (
      behavior?.kind === "http" ||
      behavior?.kind === "code" ||
      behavior?.kind === "crypto"
    ) {
      this.addBackedNode(behavior.kind, target, actionSpec!, pos);
    } else {
      this.addStep(target, action, pos);
    }
  }

  private addBackedNode(
    kind: "http" | "code" | "crypto",
    target: string,
    action: ActionSpec,
    pos?: NodePosition,
  ): void {
    const id = this.nextStepId();
    const from = this.autoConnectFrom();
    const template = (action.behavior?.template ?? {}) as NodeConfig;
    const config: NodeConfig = {
      ...defaultConfig(kind),
      ...structuredClone(template),
      stepType: `${target}.${action.name}`,
      stepLabel: action.label,
    };
    const step: Step = {
      id,
      kind,
      target: "",
      action: "",
      payload: {},
      assertions: [],
      config,
    };
    this.steps.update((list) => [...list, step]);
    this.layout.update((l) => ({ ...l, [id]: this.defaultPosition(pos) }));
    this.autoConnect(from, id);
    this.dirty.set(true);
    this.selectStep(id);
  }

  addStep(target: string, action: string, pos?: NodePosition): void {
    const id = this.nextStepId();
    const spec = this.catalog().find((t) => t.target === target);
    const actionSpec = spec?.actions.find((a) => a.name === action);
    const payload: Record<string, unknown> = {};
    for (const key of Object.keys(actionSpec?.payload_hint ?? {})) {
      payload[key] = "";
    }
    const from = this.autoConnectFrom();
    const step: Step = {
      id,
      kind: "action",
      target,
      action,
      payload,
      assertions: [],
    };
    this.steps.update((list) => [...list, step]);
    this.layout.update((l) => ({ ...l, [id]: this.defaultPosition(pos) }));
    this.autoConnect(from, id);
    this.dirty.set(true);
    this.selectStep(id);
  }

  addControlNode(kind: Exclude<NodeKind, "action">, pos?: NodePosition): void {
    const id = this.nextStepId();
    const from = this.autoConnectFrom();
    const step: Step = {
      id,
      kind,
      target: "",
      action: "",
      payload: {},
      assertions: [],
      config: defaultConfig(kind),
    };
    this.steps.update((list) => [...list, step]);
    this.layout.update((l) => ({ ...l, [id]: this.defaultPosition(pos) }));
    this.autoConnect(from, id);
    this.dirty.set(true);
    this.selectStep(id);
  }

  private nextStepId(): string {
    let max = 0;
    for (const s of this.steps()) {
      const m = /^step_(\d+)$/.exec(s.id);
      if (m) max = Math.max(max, parseInt(m[1], 10));
    }
    return `step_${String(max + 1).padStart(3, "0")}`;
  }

  selectStep(id: string): void {
    this.selectedId.set(id);
    const step = this.steps().find((s) => s.id === id);
    this.payloadText.set(step ? JSON.stringify(step.payload, null, 2) : "");
    this.payloadError.set(null);
  }

  clearSelection(): void {
    this.selectedId.set(null);
  }

  removeStep(id: string): void {
    this.steps.update((list) => list.filter((s) => s.id !== id));
    this.edges.update((list) =>
      list.filter((e) => e.source !== id && e.target !== id),
    );
    this.layout.update(({ [id]: _removed, ...rest }) => rest);
    if (this.selectedId() === id) this.selectedId.set(null);
    this.dirty.set(true);
  }

  moveStep(id: string, delta: -1 | 1): void {
    this.steps.update((list) => {
      const idx = list.findIndex((s) => s.id === id);
      const swap = idx + delta;
      if (idx < 0 || swap < 0 || swap >= list.length) return list;
      const next = [...list];
      [next[idx], next[swap]] = [next[swap], next[idx]];
      return next;
    });
    this.dirty.set(true);
  }

  // -- canvas interaction ----------------------------------------------------

  /** Screen (client) coordinates → world (canvas content) coordinates. */
  private toWorld(clientX: number, clientY: number): { x: number; y: number } {
    const el = this.canvasEl?.nativeElement;
    if (!el) return { x: clientX, y: clientY };
    const rect = el.getBoundingClientRect();
    const { x, y } = this.pan();
    const z = this.zoom();
    return {
      x: (clientX - rect.left - x) / z,
      y: (clientY - rect.top - y) / z,
    };
  }

  onNodePointerDown(event: PointerEvent, node: CanvasNode): void {
    if (event.button !== 0) return;
    event.stopPropagation();
    const w = this.toWorld(event.clientX, event.clientY);
    this.drag = {
      kind: "node",
      id: node.step.id,
      offX: w.x - node.x,
      offY: w.y - node.y,
      moved: false,
    };
  }

  onPortPointerDown(event: PointerEvent, node: CanvasNode, port: string): void {
    if (event.button !== 0) return;
    event.stopPropagation();
    const w = this.toWorld(event.clientX, event.clientY);
    this.drag = { kind: "wire", from: node.step.id, fromPort: port };
    this.wireDrag.set({ from: node.step.id, fromPort: port, x: w.x, y: w.y });
  }

  onCanvasPointerDown(event: PointerEvent): void {
    if (event.button !== 0) return;
    this.drag = {
      kind: "pan",
      startX: event.clientX,
      startY: event.clientY,
      panX: this.pan().x,
      panY: this.pan().y,
      moved: false,
    };
  }

  @HostListener("document:pointermove", ["$event"])
  onPointerMove(event: PointerEvent): void {
    const drag = this.drag;
    if (!drag) return;
    if (drag.kind === "node") {
      const w = this.toWorld(event.clientX, event.clientY);
      drag.moved = true;
      this.layout.update((l) => ({
        ...l,
        [drag.id]: {
          x: snap(w.x - drag.offX),
          y: snap(w.y - drag.offY),
        },
      }));
    } else if (drag.kind === "pan") {
      const dx = event.clientX - drag.startX;
      const dy = event.clientY - drag.startY;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      this.pan.set({ x: drag.panX + dx, y: drag.panY + dy });
    } else {
      const w = this.toWorld(event.clientX, event.clientY);
      this.wireDrag.set({
        from: drag.from,
        fromPort: drag.fromPort,
        x: w.x,
        y: w.y,
      });
    }
  }

  @HostListener("document:pointerup", ["$event"])
  onPointerUp(event: PointerEvent): void {
    const drag = this.drag;
    this.drag = null;
    if (!drag) return;
    if (drag.kind === "node") {
      if (drag.moved) this.dirty.set(true);
      else this.selectStep(drag.id);
    } else if (drag.kind === "pan") {
      if (!drag.moved) this.clearSelection();
    } else {
      const fromPort = drag.fromPort;
      this.wireDrag.set(null);
      const el = document.elementFromPoint(event.clientX, event.clientY);
      const targetId = el
        ?.closest?.("[data-node-id]")
        ?.getAttribute("data-node-id");
      if (targetId && targetId !== drag.from) {
        this.connect(drag.from, fromPort, targetId);
      }
    }
  }

  /**
   * Create (or replace) an explicit wire from a node's output port to another
   * node. A port carries at most one outgoing wire, so re-dragging it rewires.
   */
  connect(fromId: string, fromPort: string, toId: string): void {
    const err = this.connectionError(fromId, toId);
    if (err) {
      this.ui.toast("error", err);
      return;
    }
    this.edges.update((list) => {
      const kept = list.filter(
        (e) => !(e.source === fromId && e.source_port === fromPort),
      );
      return [...kept, { source: fromId, source_port: fromPort, target: toId }];
    });
    this.dirty.set(true);
  }

  /** Kind of a step by id (defaults to "action"). */
  private stepKind(id: string): string {
    return this.steps().find((s) => s.id === id)?.kind ?? "action";
  }

  /**
   * Direction validation for a wire ``fromId → toId``. A trigger node is the
   * entry (out only) and a reply node is the exit (in only), so wiring the wrong
   * way round is inconsistent. Returns an error string, or null if the wire is
   * directionally consistent.
   */
  connectionError(fromId: string, toId: string): string | null {
    if (fromId === toId) return "A step can't wire to itself.";
    if (this.stepKind(fromId) === "reply")
      return "A reply node ends the flow — it has no outgoing wire.";
    if (this.stepKind(toId) === "trigger")
      return "A trigger node starts the flow — nothing can wire into it.";
    return null;
  }

  /** How many wires currently violate direction rules (toolbar indicator). */
  readonly invalidWireCount = computed(
    () => this.wires().filter((w) => w.invalid).length,
  );

  /** Remove a wire by its index in the edge list (click-to-delete). */
  removeEdge(index: number): void {
    this.edges.update((list) => list.filter((_, i) => i !== index));
    this.dirty.set(true);
  }

  onWheel(event: WheelEvent): void {
    event.preventDefault();
    const el = this.canvasEl?.nativeElement;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const cx = event.clientX - rect.left;
    const cy = event.clientY - rect.top;
    this.zoomAt(this.zoom() * Math.exp(-event.deltaY * 0.0015), cx, cy);
  }

  zoomBy(factor: number): void {
    const el = this.canvasEl?.nativeElement;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    this.zoomAt(this.zoom() * factor, rect.width / 2, rect.height / 2);
  }

  private zoomAt(target: number, cx: number, cy: number): void {
    const old = this.zoom();
    const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, target));
    const scale = next / old;
    const { x, y } = this.pan();
    this.pan.set({ x: cx - (cx - x) * scale, y: cy - (cy - y) * scale });
    this.zoom.set(next);
  }

  zoomReset(): void {
    this.zoom.set(1);
  }

  fitView(): void {
    const nodes = this.nodes();
    const el = this.canvasEl?.nativeElement;
    if (!nodes.length || !el) return;
    const rect = el.getBoundingClientRect();
    const pad = 60;
    const minX = Math.min(...nodes.map((n) => n.x)) - pad;
    const minY = Math.min(...nodes.map((n) => n.y)) - pad;
    const maxX = Math.max(...nodes.map((n) => n.x + NODE_W)) + pad;
    const maxY = Math.max(...nodes.map((n) => n.y + NODE_H)) + pad;
    const z = Math.min(
      MAX_ZOOM,
      Math.max(
        MIN_ZOOM,
        Math.min(rect.width / (maxX - minX), rect.height / (maxY - minY), 1.25),
      ),
    );
    this.zoom.set(z);
    this.pan.set({
      x: (rect.width - (maxX - minX) * z) / 2 - minX * z,
      y: (rect.height - (maxY - minY) * z) / 2 - minY * z,
    });
  }

  targetColor(target: string): string {
    // fallback resolves to the muted text token (valid as an SVG fill)
    return (
      this.catalog().find((t) => t.target === target)?.color ??
      "var(--text-muted)"
    );
  }

  targetLabel(target: string): string {
    return this.catalog().find((t) => t.target === target)?.label ?? target;
  }

  // -- per-step connection binding ----------------------------------------

  /** Saved connections (for the inspector's Connection picker). */
  readonly connections = this.connectionsStore.connections;

  /** Saved test-data pools (for the scenario-level pool pickers). */
  readonly cardPools = this.testData.cardPools;
  readonly terminalPools = this.testData.terminalPools;

  /** Adapter steps whose target is TCP-backed can run against a saved
   * connection. Matches the registry keys wired to the universal TCP adapter. */
  private static readonly TCP_TARGETS = new Set([
    "tcp",
    "tcp_iso8583",
    "switch",
    "switch_iso8583",
    "acquirer",
    "acquirer_host",
    "hsm",
    "hsm_tcp",
    "grpc",
    "http",
    "db_probe_core",
    "db_probe_switch",
  ]);

  canBindConnection(step: Step): boolean {
    return (
      (step.kind ?? "action") === "action" &&
      ConstructorComponent.TCP_TARGETS.has(step.target)
    );
  }

  /** Saved participant groups (fleets), for the flow Call-node target picker. */
  readonly groups = this.groupsStore.groups;

  /** Connections offered for a step, filtered to the step's protocol family. In
   * flow mode, participant groups (fleets) are offered too — a flow's outbound
   * call can target a group, which the orchestrator inlines as a GroupAdapter
   * that selects a member connection per dispatch. */
  /** Adapter family a step's target binds to — drives the Connection picker. */
  private targetFamily(
    target: string,
  ): "header_echo" | "grpc" | "http" | "iso8583" | "db" {
    if (target === "hsm" || target === "hsm_tcp") return "header_echo";
    if (target === "grpc") return "grpc";
    if (target === "http") return "http";
    if (target.startsWith("db_probe")) return "db";
    return "iso8583";
  }

  /** The picker family a *connection* belongs to — keyed off its adapter first
   *  (payShield is an HSM; db probes are their own family), then protocol. This
   *  is why a payShield connection (adapter "payshield", whose leftover protocol
   *  may read "iso8583") still shows up for HSM steps. */
  private familyOfConnection(c: {
    adapter?: string;
    protocol?: string;
  }): "header_echo" | "grpc" | "http" | "iso8583" | "db" {
    const a = (c.adapter as string) || "tcp";
    if (a === "grpc") return "grpc";
    if (a === "http") return "http";
    if (a === "payshield" || a === "hsm_client") return "header_echo";
    if (a.startsWith("db_probe")) return "db";
    return c.protocol === "header_echo" ? "header_echo" : "iso8583";
  }

  connectionsFor(
    step: Step,
  ): { name: string; protocol: string; family?: string }[] {
    const fam = this.targetFamily(step.target);
    const conns = this.connections()
      // outbound calls target client connections, never inbound listeners.
      .filter((c) => c.mode !== "inbound")
      .filter((c) => this.familyOfConnection(c) === fam)
      .map((c) => ({ name: c.name, protocol: c.protocol }));
    if (!this.flowMode) return conns;
    // Groups are typed: only offer groups whose adapter family matches this
    // step's family (an ISO 8583 group must not appear for an HSM step).
    const groups = this.groups()
      .filter((g) => this.groupFamily(g) === fam)
      .map((g) => ({
        name: g.name,
        protocol: "group",
        family: this.groupFamily(g),
      }));
    return [...conns, ...groups];
  }

  /** A group's adapter family — its stamped ``adapter_type`` (grpc/http/iso8583/
   * header_echo), or derived from its first member connection for legacy groups. */
  private groupFamily(g: {
    adapter_type?: string;
    members?: { connection: string }[];
  }): string {
    // Derive from the actual members first (adapter-aware), so a group whose
    // stamped ``adapter_type`` is stale (e.g. an HSM group reading "iso8583")
    // is still classified — and offered — correctly.
    const first = (g.members || [])[0];
    const c = first
      ? this.connections().find((x) => x.name === first.connection)
      : undefined;
    if (c) return this.familyOfConnection(c);
    const t = g.adapter_type;
    if (t === "payshield") return "header_echo";
    if (t && t.startsWith("db_probe")) return "db";
    return t || "iso8583";
  }

  setStepConnection(step: Step, name: string): void {
    this.updateConfig(step, { connection: name || undefined });
  }

  // -- relay / proxy node --------------------------------------------------

  /** Upstream targets a relay node can forward to: any outbound connection, or a
   *  participant group (fleet) — the proxy balances connections across members. */
  upstreamConnections(): {
    name: string;
    host?: string;
    port?: number;
    group?: boolean;
    members?: number;
  }[] {
    const conns = this.connections()
      .filter((c) => c.mode !== "inbound")
      .map((c) => ({ name: c.name, host: c.host, port: c.port }));
    const groups = this.groups().map((g) => ({
      name: g.name,
      group: true,
      members: (g.members || []).length,
    }));
    return [...conns, ...groups];
  }

  /** Relay forwarding posture: ``decode`` (parse each command for visibility,
   *  forward all) or ``raw`` (blind byte-exact passthrough). */
  relayMode(step: Step): string {
    return step.config?.relay_mode || "decode";
  }

  setRelayMode(step: Step, mode: string): void {
    this.updateConfig(step, { relay_mode: mode === "raw" ? "raw" : "decode" });
  }

  /** Client-side check: does this step reference a step that isn't earlier? */
  stepHasRefError(step: Step, index: number): boolean {
    const earlier = new Set(
      this.steps()
        .slice(0, index)
        .map((s) => s.id),
    );
    const text = JSON.stringify(step.payload);
    for (const match of text.matchAll(VAR_REF_RE)) {
      if (!earlier.has(match[1])) return true;
    }
    return false;
  }

  // -- node geometry & control helpers -------------------------------------

  nodeHeight(step: Step): number {
    const ports = nodeOutputPorts(step).length;
    return Math.max(NODE_H, ports * PORT_GAP + 28);
  }

  isControl(step: Step | null | undefined): boolean {
    return !!step && step.kind !== undefined && step.kind !== "action";
  }

  /** One-line summary shown on a control node's body. */
  controlSummary(step: Step): string {
    const c = step.config ?? {};
    switch (step.kind) {
      case "if":
        return `${c.left || "?"} ${c.operator || "eq"} ${this.valueLabel(c.right)}`;
      case "switch":
        return `on ${c.value || "?"} · ${(c.cases ?? []).length} case(s)`;
      case "loop":
        if (c.mode === "forEach") return `for each ${c.list || "?"}`;
        if (c.mode === "while")
          return `while ${c.condition?.left || "?"} ${c.condition?.operator || "eq"} …`;
        return `repeat ${c.count ?? 0}×`;
      case "wait":
        return `${c.ms ?? 0} ms`;
      case "merge":
        return "join branches";
      case "code": {
        const lines = (c.code ?? "").split("\n").filter((l) => l.trim()).length;
        return `${c.language ?? "python"} · ${lines} line${lines === 1 ? "" : "s"}`;
      }
      case "http":
        return `${c.method ?? "GET"} ${c.url || "—"}`;
      case "init":
        return "initial JSON";
      case "crypto":
        return c.operation ?? "crypto";
      case "call":
        return c.scenario_id
          ? this.scenarioName(c.scenario_id)
          : "pick a scenario";
      default:
        return "";
    }
  }

  /** Display name for a referenced sub-scenario id (falls back to the id). */
  scenarioName(id: string): string {
    return this.scenarioList().find((s) => s.id === id)?.name ?? id;
  }

  private valueLabel(v: unknown): string {
    if (v === undefined || v === null || v === "") return '""';
    return typeof v === "string" ? v : JSON.stringify(v);
  }

  /** Template-accessible value formatter (assertions display). */
  valueLabelPub(v: unknown): string {
    return this.valueLabel(v);
  }

  // -- control-node config -------------------------------------------------

  updateConfig(step: Step, patch: Partial<NodeConfig>): void {
    this.updateStep(step.id, { config: { ...(step.config ?? {}), ...patch } });
  }

  updateCondition(step: Step, patch: Partial<Condition>): void {
    const current: Condition = step.config?.condition ?? {
      left: "",
      operator: "eq",
      right: "",
    };
    this.updateConfig(step, { condition: { ...current, ...patch } });
  }

  onLoopModeChange(step: Step, mode: NodeConfig["mode"]): void {
    const patch: Partial<NodeConfig> = { mode };
    if (mode === "while" && !step.config?.condition) {
      patch.condition = { left: "", operator: "eq", right: "" };
    }
    this.updateConfig(step, patch);
  }

  addSwitchCase(step: Step): void {
    this.updateConfig(step, {
      cases: [...(step.config?.cases ?? []), { value: "" }],
    });
  }

  removeSwitchCase(step: Step, index: number): void {
    const cases = (step.config?.cases ?? []).filter((_, i) => i !== index);
    // dropping a case shifts port ids — drop wires off the removed port
    const removedPort = `case_${(step.config?.cases ?? []).length - 1}`;
    this.edges.update((list) =>
      list.filter(
        (e) => !(e.source === step.id && e.source_port === removedPort),
      ),
    );
    this.updateConfig(step, { cases });
  }

  updateSwitchCase(step: Step, index: number, value: string): void {
    const cases = (step.config?.cases ?? []).map((c, i) =>
      i === index ? { value } : c,
    );
    this.updateConfig(step, { cases });
  }

  /** Coerce a raw text field into a typed value (number/bool/json or string). */
  parseScalar(raw: string): unknown {
    try {
      return JSON.parse(raw);
    } catch {
      return raw;
    }
  }

  // -- HTTP node config ----------------------------------------------------

  updateAuth(step: Step, patch: Partial<HttpAuth>): void {
    const current: HttpAuth = step.config?.authentication ?? { type: "none" };
    this.updateConfig(step, { authentication: { ...current, ...patch } });
  }

  updateOptions(step: Step, patch: Partial<HttpOptions>): void {
    const current: HttpOptions = step.config?.options ?? {};
    this.updateConfig(step, { options: { ...current, ...patch } });
  }

  /** The KeyValue[] config keys the HTTP panel edits. */
  private rows(step: Step, key: HttpRowKey): KeyValue[] {
    return (step.config?.[key] as KeyValue[] | undefined) ?? [];
  }

  addRow(step: Step, key: HttpRowKey): void {
    this.updateConfig(step, {
      [key]: [...this.rows(step, key), { name: "", value: "" }],
    } as Partial<NodeConfig>);
  }

  removeRow(step: Step, key: HttpRowKey, i: number): void {
    this.updateConfig(step, {
      [key]: this.rows(step, key).filter((_, j) => j !== i),
    } as Partial<NodeConfig>);
  }

  updateRow(
    step: Step,
    key: HttpRowKey,
    i: number,
    patch: Partial<KeyValue>,
  ): void {
    this.updateConfig(step, {
      [key]: this.rows(step, key).map((r, j) =>
        j === i ? { ...r, ...patch } : r,
      ),
    } as Partial<NodeConfig>);
  }

  // -- code-node inputs (resolvable, chainable) ----------------------------

  private codeInputs(step: Step): KeyValue[] {
    return step.config?.inputs ?? [];
  }

  addInputRow(step: Step): void {
    this.updateConfig(step, {
      inputs: [...this.codeInputs(step), { name: "", value: "" }],
    });
  }

  removeInputRow(step: Step, i: number): void {
    this.updateConfig(step, {
      inputs: this.codeInputs(step).filter((_, j) => j !== i),
    });
  }

  updateInputRow(step: Step, i: number, patch: Partial<KeyValue>): void {
    this.updateConfig(step, {
      inputs: this.codeInputs(step).map((r, j) =>
        j === i ? { ...r, ...patch } : r,
      ),
    });
  }

  /** Registered Message Formats for a given protocol (for the input picker). */
  formatsFor(protocol: string): MessageFormat[] {
    return this.messageFormats().filter((f) => f.protocol === protocol);
  }

  /**
   * Snapshot a chosen Message Format's definition into a code-node input. For
   * ISO8583 the field table goes into the value (what the snippet runs); the
   * id/version are recorded so the editor can show what was snapshotted.
   */
  applyFormat(step: Step, i: number, formatId: string): void {
    if (!formatId) {
      this.updateInputRow(step, i, { formatId: "", formatVersion: "" });
      return;
    }
    const fmt = this.messageFormats().find((f) => f.id === formatId);
    if (!fmt) return;
    const def = (fmt.definition ?? {}) as Record<string, unknown>;
    const value =
      fmt.protocol === "iso8583"
        ? JSON.stringify(def["fields"] ?? {})
        : JSON.stringify(def);
    this.updateInputRow(step, i, {
      value,
      formatId: fmt.id,
      formatVersion: fmt.version,
    });
  }

  /** How many data elements a snapshotted ISO8583 field table holds. */
  fieldCount(row: KeyValue): number {
    try {
      const parsed = JSON.parse(row.value || "{}");
      return parsed && typeof parsed === "object"
        ? Object.keys(parsed).length
        : 0;
    } catch {
      return 0;
    }
  }

  // -- call-node variable overrides ----------------------------------------

  private callVars(step: Step): KeyValue[] {
    return step.config?.variables ?? [];
  }
  addCallVar(step: Step): void {
    this.updateConfig(step, {
      variables: [...this.callVars(step), { name: "", value: "" }],
    });
  }
  removeCallVar(step: Step, i: number): void {
    this.updateConfig(step, {
      variables: this.callVars(step).filter((_, j) => j !== i),
    });
  }
  updateCallVar(step: Step, i: number, patch: Partial<KeyValue>): void {
    this.updateConfig(step, {
      variables: this.callVars(step).map((r, j) =>
        j === i ? { ...r, ...patch } : r,
      ),
    });
  }

  // -- node detail modal & single-node execution ---------------------------

  readonly detailNodeId = signal<string | null>(null);
  readonly executing = signal(false);
  /** Last execution result per node id (also drives the OUTPUT pane). */
  readonly nodeResults = signal<Record<string, ExecuteNodeResult>>({});
  /** Pinned node outputs — used as mock input so downstream runs skip re-running them. */
  readonly pinned = signal<Record<string, ExecuteNodeResult>>({});

  isPinned(id: string): boolean {
    return Object.prototype.hasOwnProperty.call(this.pinned(), id);
  }

  /** Pin the current output of a node (or unpin it). Pinned nodes are reused. */
  togglePin(step: Step): void {
    const id = step.id;
    if (this.isPinned(id)) {
      this.pinned.update(({ [id]: _removed, ...rest }) => rest);
      this.ui.toast("success", `Unpinned ${id}`);
      return;
    }
    const r = this.nodeResults()[id];
    if (!r) {
      this.ui.toast("error", "Run this node first, then pin its output.");
      return;
    }
    this.pinned.update((m) => ({ ...m, [id]: r }));
    this.ui.toast("success", `Pinned ${id} output (reused without re-running)`);
  }

  /** Build a run context for `id` from its ancestors, preferring pinned outputs. */
  private collectContext(id: string): Record<string, unknown> {
    const ctx: Record<string, unknown> = {};
    for (const ancestor of this.ancestors(id)) {
      const r = this.pinned()[ancestor] ?? this.nodeResults()[ancestor];
      if (r)
        ctx[ancestor] = {
          request: r.request ?? {},
          response: r.response ?? {},
        };
    }
    return ctx;
  }

  readonly detailStep = computed(
    () => this.steps().find((s) => s.id === this.detailNodeId()) ?? null,
  );

  /** Upstream node outputs feeding the open node (its INPUT pane / exec context). */
  readonly detailInput = computed<Record<string, unknown>>(() => {
    const id = this.detailNodeId();
    if (!id) return {};
    return this.collectContext(id);
  });

  readonly detailOutput = computed<ExecuteNodeResult | null>(
    () => this.nodeResults()[this.detailNodeId() ?? ""] ?? null,
  );

  openDetail(id: string): void {
    this.selectStep(id);
    this.detailNodeId.set(id);
  }

  closeDetail(): void {
    this.detailNodeId.set(null);
  }

  /** Node ids that can reach `id` along edges (transitive predecessors). */
  private ancestors(id: string): string[] {
    const incoming = new Map<string, string[]>();
    for (const e of this.edges()) {
      incoming.set(e.target, [...(incoming.get(e.target) ?? []), e.source]);
    }
    const seen = new Set<string>();
    const order: string[] = [];
    const visit = (n: string) => {
      for (const p of incoming.get(n) ?? []) {
        if (!seen.has(p)) {
          seen.add(p);
          visit(p);
          order.push(p);
        }
      }
    };
    visit(id);
    return order;
  }

  /** Strip a step down to what the execute endpoint needs. */
  private nodeForExec(step: Step): unknown {
    return {
      id: step.id,
      kind: step.kind ?? "action",
      target: step.target,
      action: step.action,
      payload: step.payload,
      assertions: step.assertions,
      config: step.config ?? {},
    };
  }

  executeNode(step: Step): void {
    this.executing.set(true);
    this.runApi
      .executeNode({
        node: this.nodeForExec(step),
        context: this.detailInput(),
      })
      .subscribe({
        next: (res) => {
          this.executing.set(false);
          this.nodeResults.update((m) => ({ ...m, [step.id]: res }));
          if (res.status === "passed") {
            this.ui.toast("success", `${step.id} ran — ${res.status}`);
          } else {
            this.ui.toast("error", res.error || `${step.id} ${res.status}`);
          }
        },
        error: () => {
          this.executing.set(false);
          this.ui.toast(
            "error",
            "Execute failed — is the orchestrator running (port 8100)?",
          );
        },
      });
  }

  /** Run every upstream node in order, then the node itself (pinned ones are reused). */
  async executeUpstream(step: Step): Promise<void> {
    const chain = [...this.ancestors(step.id), step.id];
    this.executing.set(true);
    for (const id of chain) {
      if (this.isPinned(id)) continue; // reuse pinned output, don't re-run
      const node = this.steps().find((s) => s.id === id);
      if (!node) continue;
      try {
        const res = await this.runOne(node);
        this.nodeResults.update((m) => ({ ...m, [id]: res }));
        if (res.status !== "passed") break;
      } catch {
        break;
      }
    }
    this.executing.set(false);
  }

  private runOne(step: Step): Promise<ExecuteNodeResult> {
    const ctx = this.collectContext(step.id);
    return new Promise((resolve, reject) => {
      this.runApi
        .executeNode({ node: this.nodeForExec(step), context: ctx })
        .subscribe({ next: resolve, error: reject });
    });
  }

  asJson(value: unknown): string {
    try {
      return JSON.stringify(value ?? {}, null, 2);
    } catch {
      return String(value);
    }
  }

  // -- scenario preview run (Execute) & live debugging ---------------------

  readonly environments = signal<EnvironmentInfo[]>([]);
  readonly selectedEnv = signal<string>("mock");
  /** Cached global tables, attached to preview runs as scenario.tables. */
  readonly tablesRuntime = signal<Record<string, Record<string, unknown>[]>>(
    {},
  );
  /** Data-driven run: when set, fan the scenario over this table's rows. */
  readonly dataTable = signal<string>("");
  /** Names of global tables available for a data-driven run. */
  readonly tableNames = computed(() =>
    Object.keys(this.tablesRuntime()).sort(),
  );
  /** Row count of the selected data table (0 ⇒ single run). */
  readonly dataRowCount = computed(
    () => (this.tablesRuntime()[this.dataTable()] ?? []).length,
  );
  readonly running = signal(false);
  readonly runId = signal<string | null>(null);
  readonly runResultStatus = signal<RunStatus | null>(null);
  /** Per-node live status during/after a run: step id -> status. */
  readonly runStatus = signal<Record<string, RunStatus>>({});
  /** The node currently executing (last step.result received). */
  readonly activeNode = signal<string | null>(null);
  /** Ordered per-step outcomes for the results panel (live). */
  readonly runLog = signal<StepResult[]>([]);
  /** Whether the bottom results panel is open. */
  readonly showResults = signal(false);
  private runSub: { unsubscribe(): void } | null = null;

  // -- step-through debugger --
  /** Node ids with a breakpoint. */
  readonly breakpoints = signal<Set<string>>(new Set());
  /** True while a debug run is active. */
  readonly debugging = signal(false);
  /** The node the debugger is currently paused at (or null). */
  readonly pausedNode = signal<string | null>(null);

  isBreakpoint(id: string): boolean {
    return this.breakpoints().has(id);
  }

  toggleBreakpoint(id: string): void {
    this.breakpoints.update((s) => {
      const next = new Set(s);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  debugStep(): void {
    const id = this.runId();
    if (id)
      this.runApi.debugStep(id).subscribe({ next: () => {}, error: () => {} });
  }

  debugContinue(): void {
    const id = this.runId();
    if (id)
      this.runApi
        .debugContinue(id)
        .subscribe({ next: () => {}, error: () => {} });
  }

  debugStop(): void {
    const id = this.runId();
    if (id)
      this.runApi.debugStop(id).subscribe({ next: () => {}, error: () => {} });
    this.pausedNode.set(null);
    this.debugging.set(false);
  }

  // -- flow debug (test-fire a participant flow against a sample request) --

  /** Editable sample inbound message for a flow test-fire / debug run. Seeded
   * into the flow context as ${request.*} — what a decoded wire message would
   * look like (ISO: mti + de map; HSM: command; HTTP: method/path/body). */
  readonly flowRequestJson = signal<string>("");
  /** Whether the sample-request editor panel is open. */
  readonly showFlowRequest = signal(false);
  /** True once the user typed in the sample-request box — stop re-seeding. */
  private flowRequestEdited = false;

  /** A protocol-appropriate sample request for this flow, derived from its
   * trigger connection's adapter family. Getting the shape right matters:
   * e.g. a relay node forwards the inbound request as-is, and an HSM upstream
   * expects {command,data} — an ISO-shaped sample would be forwarded as a
   * garbage 2-char command. */
  private defaultFlowRequest(): Record<string, unknown> {
    const conn = this.connections().find((c) => c.name === this.flowTrigger);
    if (conn?.adapter === "http") {
      return { method: "POST", path: "/", body: {} };
    }
    if (conn?.protocol === "header_echo" || conn?.adapter === "payshield") {
      return { command: "NC", data: "" };
    }
    return {
      mti: "0200",
      de: {
        "2": "4111111111111111",
        "3": "000000",
        "4": "000000010000",
        "11": "000001",
      },
    };
  }

  /** Re-seed the sample to match the trigger connection until the user edits. */
  private seedFlowRequest(): void {
    if (this.flowRequestEdited && this.flowRequestJson().trim()) return;
    this.flowRequestJson.set(
      JSON.stringify(this.defaultFlowRequest(), null, 2),
    );
  }

  setFlowRequestJson(text: string): void {
    this.flowRequestEdited = true;
    this.flowRequestJson.set(text);
  }

  toggleFlowRequest(): void {
    this.seedFlowRequest();
    this.showFlowRequest.update((v) => !v);
  }

  /** Run this participant flow once against the sample request. With debug
   * true it pauses at breakpoints (or every node when none are set) and is
   * driven by the same Step/Continue controls as a scenario debug run. */
  executeFlow(debug = true): void {
    if (this.running() || !this.flowMode) return;
    const doc = this.buildFlowDoc();
    if (!doc) return;
    if (!doc.nodes.length) {
      this.ui.toast("error", "Nothing to run — add a node first.");
      return;
    }
    this.seedFlowRequest();
    let request: Record<string, unknown>;
    try {
      request = JSON.parse(this.flowRequestJson() || "{}");
    } catch {
      this.showFlowRequest.set(true);
      this.ui.toast(
        "error",
        "Sample request is not valid JSON — fix it first.",
      );
      return;
    }
    // reset previous run state (mirrors execute())
    this.runStatus.set({});
    this.activeNode.set(null);
    this.runResultStatus.set(null);
    this.nodeResults.set({});
    this.runLog.set([]);
    this.showResults.set(true);
    this.pausedNode.set(null);
    this.debugging.set(debug);
    this.running.set(true);
    this.runApi
      .startFlowDebug({
        flow: { ...doc, id: this.flowId ?? undefined } as Record<
          string,
          unknown
        >,
        request,
        debug,
        breakpoints: [...this.breakpoints()],
      })
      .subscribe({
        next: (res) => this.streamRun(res.run_id),
        error: (e) => {
          this.running.set(false);
          this.debugging.set(false);
          this.ui.toast(
            "error",
            this.httpErrorText(e) ||
              "Could not start the flow run — is the orchestrator running (port 8100)?",
          );
        },
      });
  }

  readonly runCounts = computed(() => {
    const log = this.runLog();
    return {
      total: log.length,
      passed: log.filter((s) => s.status === "passed").length,
      failed: log.filter((s) => s.status === "failed" || s.status === "error")
        .length,
    };
  });

  toggleResults(): void {
    this.showResults.update((v) => !v);
  }

  /** Normalised assertion rows from a run outcome (live event or run detail). */
  assertionRows(a: unknown): {
    field: string;
    operator: string;
    expected: unknown;
    actual: unknown;
    passed: boolean;
  }[] {
    return (Array.isArray(a) ? a : []) as {
      field: string;
      operator: string;
      expected: unknown;
      actual: unknown;
      passed: boolean;
    }[];
  }

  /** Human-readable reason a step failed: its error, else its failed assertions. */
  failReason(s: { error?: string | null; assertions?: unknown }): string {
    if (s.error) return s.error;
    const failed = this.assertionRows(s.assertions).filter(
      (a) => a.passed === false,
    );
    if (failed.length) {
      return failed
        .map(
          (a) =>
            `${a.field} ${a.operator} ${this.valueLabel(a.expected)} (got ${this.valueLabel(a.actual)})`,
        )
        .join("; ");
    }
    return "";
  }

  private loadEnvironments(): void {
    this.runApi.environments().subscribe({
      next: (envs) => {
        this.environments.set(envs);
        if (envs.length && !envs.some((e) => e.name === this.selectedEnv())) {
          this.selectedEnv.set(envs[0].name);
        }
      },
      error: () => this.environments.set([]),
    });
    this.api.tablesRuntime().subscribe({
      next: (t) => this.tablesRuntime.set(t),
      error: () => this.tablesRuntime.set({}),
    });
  }

  nodeRunStatus(stepId: string): RunStatus | null {
    return this.runStatus()[stepId] ?? null;
  }

  /** Run this scenario through the orchestrator and stream results live. */
  execute(debug = false): void {
    if (this.running()) return;
    const draft = this.buildDraft();
    if (!draft.steps.length) {
      this.ui.toast("error", "Nothing to run — add a node first.");
      return;
    }
    // reset previous run state
    this.runStatus.set({});
    this.activeNode.set(null);
    this.runResultStatus.set(null);
    this.nodeResults.set({});
    this.runLog.set([]);
    this.showResults.set(true);
    this.pausedNode.set(null);
    this.debugging.set(debug);
    this.running.set(true);

    // Merge global/project/set variables under the scenario's own, so a
    // preview run sees the same ${vars.NAME} a real run would.
    this.api
      .resolveVariables({
        project_id: this.currentProjectId(),
        set_ids: this.memberSetIds(),
        variables: draft.variables ?? {},
        secret_vars: draft.secret_vars ?? [],
      })
      .subscribe({
        next: (res) =>
          this.startPreview(draft, res.effective, res.secret_values),
        error: () => this.startPreview(draft, draft.variables ?? {}, []),
      });
  }

  private startPreview(
    draft: ScenarioDraft,
    variables: Record<string, unknown>,
    secretValues: string[] = [],
  ): void {
    const scenario = {
      ...draft,
      variables,
      __secrets__: secretValues,
      tables: this.tablesRuntime(),
      id: this.scenarioId ?? "preview",
      name: this.meta().name || "preview",
    };
    const table = this.dataTable();
    const rows = table ? this.dataRowCount() : 0;
    this.runApi
      .start({
        scenarios: [scenario as unknown as Record<string, unknown>],
        environment_name: this.selectedEnv(),
        label:
          `${this.debugging() ? "debug" : "preview"}: ${scenario.name}` +
          (rows ? ` × ${rows} rows` : ""),
        debug: this.debugging(),
        breakpoints: [...this.breakpoints()],
        ...(table && !this.debugging() ? { data_table: table } : {}),
      })
      .subscribe({
        next: (res) => this.streamRun(res.run_id),
        error: () => {
          this.running.set(false);
          this.ui.toast(
            "error",
            "Could not start run — is the orchestrator running (port 8100)?",
          );
        },
      });
  }

  private streamRun(runId: string): void {
    this.runId.set(runId);
    this.runSub = this.runMonitor.stream(runId).subscribe({
      next: (event) => {
        if (event.type === "step.result") {
          const data = event.data as unknown as StepResult;
          this.activeNode.set(data.step);
          this.runStatus.update((m) => ({ ...m, [data.step]: data.status }));
          this.runLog.update((log) => [...log, data]);
        } else if (event.type === "debug.paused") {
          const data = event.data as {
            node: string;
            context?: Record<string, unknown>;
          };
          this.pausedNode.set(data.node);
          this.activeNode.set(data.node);
          // populate node outputs from the paused-state snapshot
          const ctx = data.context ?? {};
          const results: Record<string, ExecuteNodeResult> = {
            ...this.nodeResults(),
          };
          for (const [id, v] of Object.entries(ctx)) {
            if (id === "vars" || id === "tables") continue;
            const r = v as { request?: unknown; response?: unknown };
            results[id] = {
              ok: true,
              status: "passed",
              request: r.request,
              response: r.response,
            };
          }
          this.nodeResults.set(results);
        } else if (event.type === "debug.resumed") {
          this.pausedNode.set(null);
        } else if (event.type === "run.completed") {
          const data = event.data as { status?: RunStatus };
          this.runResultStatus.set(data.status ?? "passed");
        }
      },
      error: () => {
        this.running.set(false);
        this.ui.toast("error", "Lost connection to the run stream.");
      },
      complete: () => {
        this.running.set(false);
        this.activeNode.set(null);
        this.pausedNode.set(null);
        this.debugging.set(false);
        this.fetchRunOutputs(runId);
      },
    });
  }

  /** After completion, pull full per-step request/response into nodeResults. */
  private fetchRunOutputs(runId: string): void {
    this.runApi.get(runId).subscribe({
      next: (detail) => {
        const scenario = detail.summary?.scenarios?.[0];
        if (!scenario) return;
        const results: Record<string, ExecuteNodeResult> = {};
        const status: Record<string, RunStatus> = {};
        for (const step of scenario.steps) {
          results[step.step_id] = {
            ok: step.status === "passed",
            status: step.status as ExecuteNodeResult["status"],
            request: step.request,
            response: step.response,
            error: step.error,
            duration_ms: step.duration_ms,
            assertions: step.assertions,
          };
          status[step.step_id] = step.status;
        }
        this.nodeResults.set(results);
        this.runStatus.set(status);
        const counts = detail.summary?.scenarios?.[0]?.status;
        this.ui.toast(
          this.runResultStatus() === "passed" ? "success" : "error",
          `Run ${this.runResultStatus() ?? counts} — ${scenario.steps.length} steps`,
        );
      },
      error: () => {},
    });
  }

  stopRun(): void {
    const id = this.runId();
    if (id)
      this.runApi.cancel(id).subscribe({ next: () => {}, error: () => {} });
    this.runSub?.unsubscribe();
    this.running.set(false);
    this.activeNode.set(null);
  }

  clearRun(): void {
    this.runStatus.set({});
    this.runResultStatus.set(null);
    this.activeNode.set(null);
  }

  // -- config panel --------------------------------------------------------

  updateStep(id: string, patch: Partial<Step>): void {
    this.steps.update((list) =>
      list.map((s) => (s.id === id ? { ...s, ...patch } : s)),
    );
    this.dirty.set(true);
  }

  onStepIdChange(oldId: string, newId: string): void {
    const trimmed = newId.trim();
    if (!trimmed || trimmed === oldId) return;
    if (this.steps().some((s) => s.id === trimmed)) return;
    this.steps.update((list) =>
      list.map((s) => (s.id === oldId ? { ...s, id: trimmed } : s)),
    );
    this.edges.update((list) =>
      list.map((e) => ({
        ...e,
        source: e.source === oldId ? trimmed : e.source,
        target: e.target === oldId ? trimmed : e.target,
      })),
    );
    this.layout.update(({ [oldId]: pos, ...rest }) =>
      pos ? { ...rest, [trimmed]: pos } : rest,
    );
    if (this.selectedId() === oldId) this.selectedId.set(trimmed);
    this.dirty.set(true);
  }

  onActionChange(step: Step, action: string): void {
    this.updateStep(step.id, { action });
  }

  onPayloadInput(text: string): void {
    this.payloadText.set(text);
    const step = this.selectedStep();
    if (!step) return;
    try {
      const parsed = JSON.parse(text);
      if (
        parsed === null ||
        typeof parsed !== "object" ||
        Array.isArray(parsed)
      ) {
        this.payloadError.set("Payload must be a JSON object.");
        return;
      }
      this.payloadError.set(null);
      this.updateStep(step.id, { payload: parsed });
    } catch {
      this.payloadError.set("Invalid JSON — changes not applied.");
    }
  }

  insertRef(ref: string): void {
    const area = this.payloadArea?.nativeElement;
    if (!area) return;
    const start = area.selectionStart ?? area.value.length;
    const end = area.selectionEnd ?? start;
    const next = area.value.slice(0, start) + ref + area.value.slice(end);
    this.onPayloadInput(next);
    setTimeout(() => {
      area.focus();
      area.selectionStart = area.selectionEnd = start + ref.length;
    });
  }

  addAssertion(step: Step): void {
    this.updateStep(step.id, {
      assertions: [
        ...step.assertions,
        { field: "", operator: "eq", expected: "" },
      ],
    });
  }

  removeAssertion(step: Step, index: number): void {
    this.updateStep(step.id, {
      assertions: step.assertions.filter((_, i) => i !== index),
    });
  }

  updateAssertion(step: Step, index: number, patch: Partial<Assertion>): void {
    const assertions = step.assertions.map((a, i) =>
      i === index ? { ...a, ...patch } : a,
    );
    this.updateStep(step.id, { assertions });
  }

  onExpectedChange(step: Step, index: number, raw: string): void {
    let value: unknown = raw;
    try {
      value = JSON.parse(raw);
    } catch {
      /* keep as plain string */
    }
    this.updateAssertion(step, index, { expected: value });
  }

  onOperatorChange(step: Step, index: number, op: string): void {
    const operator = op as Operator;
    const patch: Partial<Assertion> = { operator };
    if (UNARY_OPERATORS.has(operator)) patch.expected = undefined;
    this.updateAssertion(step, index, patch);
  }

  isUnary(op: string): boolean {
    return UNARY_OPERATORS.has(op);
  }

  expectedAsString(value: unknown): string {
    if (value === undefined || value === null) return "";
    return typeof value === "string" ? value : JSON.stringify(value);
  }

  issueClick(stepId: string | null | undefined): void {
    if (stepId) this.selectStep(stepId);
  }
}
