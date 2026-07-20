import {
  Component,
  OnDestroy,
  OnInit,
  inject,
  signal,
  computed,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { ActivatedRoute } from "@angular/router";

import { UiService } from "../shared/ui.service";
import { ThemeService } from "../shared/theme.service";
import { PagePoll } from "../shared/poll-health";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";
import { ParticipantsService } from "../participants/participants.service";
import { ScenarioApiService } from "../constructor/scenario-api.service";
import { ScenarioSummary } from "../constructor/scenario.models";
import { EnvironmentsService } from "../environments/environments.service";
import { GroupsService } from "../groups/groups.service";
import { RunApiService, SavedSimulator } from "../run-monitor/run-api.service";
import {
  NetworkEdge,
  NetworkFlow,
  NetworkFlowIssue,
  NetworkFlowPlan,
  NetworkFlowsService,
  NetworkNode,
} from "./network-flows.service";

/** Canvas node box dimensions. */
const NODE_W = 172;
const NODE_H = 56;

interface NetDraft {
  id?: string;
  name: string;
  description: string;
  nodes: NetworkNode[];
  edges: NetworkEdge[];
  layout: Record<string, { x: number; y: number }>;
}

/**
 * Network Flows (ADR-0004) — the canvas-authored successor of Topologies.
 * Participants and initiator scenarios are placed as nodes; edges are *wiring*
 * ("source sends traffic to target" — callees start first, derived by
 * topological sort). Start/stop brings the whole simulated network up/down.
 */
@Component({
  selector: "app-network-flows",
  standalone: true,
  imports: [
    FormsModule,
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    BadgeComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="nf">
      <pp-page-header
        subtitle="Compose participants, simulators and traffic drivers on a canvas and start the whole simulated network as one unit. Wiring means “sends traffic to” — start order is derived from it (callees first), or use Auto-wire to draw it from what the flows already declare."
      >
        <pp-button variant="primary" icon="plus" (click)="newFlow()"
          >New network</pp-button
        >
        <button
          type="button"
          class="iconbtn"
          (click)="theme.toggle()"
          title="Toggle theme"
        >
          <pp-icon
            [name]="theme.theme() === 'dark' ? 'sun' : 'moon'"
            [size]="18"
          />
        </button>
      </pp-page-header>

      <div class="nf__body">
        <aside class="nf__list">
          <div class="nf__list-head">
            <span>{{ svc.flows().length }} network(s)</span>
            <span class="spacer"></span>
            <button class="iconbtn" (click)="refresh()" title="Refresh">
              <pp-icon name="activity" [size]="16" />
            </button>
          </div>
          @for (f of svc.flows(); track f.id) {
            <button
              class="row"
              [class.active]="draft()?.id === f.id"
              (click)="open(f.id)"
            >
              <div class="row__main">
                <strong>{{ f.name }}</strong>
                <small class="muted"
                  >{{ f.nodes.length || 0 }} node(s) ·
                  {{ f.edges.length || 0 }} wire(s)</small
                >
              </div>
              @if (svc.runFor(f.id); as r) {
                <pp-badge tone="success">● up</pp-badge>
              }
            </button>
          } @empty {
            <div class="nf__onboard">
              <p class="muted">No networks yet.</p>
              <p class="muted small">
                See PayProbe end to end in one click — this builds a demo
                acquiring network (a switch, 3 issuers and a payShield HSM) you
                can start, trace and stress right away.
              </p>
              <pp-button
                variant="primary"
                icon="sparkles"
                [disabled]="installing()"
                (click)="loadShowcase()"
              >
                {{ installing() ? "Loading…" : "Load showcase network" }}
              </pp-button>
              <p class="muted small">
                …or create an empty one with “New network”.
              </p>
            </div>
          }
        </aside>

        <section class="nf__editor">
          @if (draft(); as d) {
            <div class="bar">
              <input
                class="bar__name"
                type="text"
                [(ngModel)]="d.name"
                placeholder="network name"
              />
              <input
                class="bar__desc"
                type="text"
                [(ngModel)]="d.description"
                placeholder="description"
              />
              <span class="spacer"></span>
              @if (running(); as r) {
                <pp-badge tone="success"
                  >● running {{ r.health?.live ?? "?" }}/{{
                    r.health?.total ?? "?"
                  }}</pp-badge
                >
                <pp-button
                  variant="danger"
                  size="sm"
                  icon="x"
                  (click)="stop(r.id)"
                  >Stop</pp-button
                >
              } @else {
                <pp-button
                  variant="secondary"
                  size="sm"
                  icon="play"
                  (click)="start()"
                  >Start</pp-button
                >
              }
              <pp-button
                variant="primary"
                size="sm"
                icon="check"
                (click)="save()"
                >{{ d.id ? "Save" : "Create" }}</pp-button
              >
            </div>

            <div class="bar bar--tools">
              <pp-button
                variant="ghost"
                size="sm"
                icon="layers"
                (click)="addParticipant()"
                >Add participant</pp-button
              >
              <pp-button
                variant="ghost"
                size="sm"
                icon="zap"
                (click)="addInitiator()"
                >Add initiator</pp-button
              >
              <pp-button
                variant="ghost"
                size="sm"
                icon="server"
                (click)="addSimulator()"
                >Add simulator</pp-button
              >
              <pp-button
                variant="ghost"
                size="sm"
                icon="group"
                (click)="addGroup()"
                >Add group</pp-button
              >
              <pp-button
                [variant]="wireMode() ? 'secondary' : 'ghost'"
                size="sm"
                icon="plug"
                (click)="toggleWireMode()"
              >
                {{
                  wireMode()
                    ? wireSource()
                      ? "pick target…"
                      : "pick source…"
                    : "Wire"
                }}
              </pp-button>
              <pp-button
                variant="ghost"
                size="sm"
                icon="sparkles"
                (click)="autoWire()"
                >Auto-wire</pp-button
              >
              <pp-button
                [variant]="showPlan() ? 'secondary' : 'ghost'"
                size="sm"
                icon="list"
                (click)="togglePlan()"
                >Plan</pp-button
              >
              @if (selectedEdge() !== null) {
                <pp-button
                  variant="ghost"
                  size="sm"
                  icon="x"
                  (click)="deleteSelectedEdge()"
                  >Remove wire</pp-button
                >
              }
              <span class="spacer"></span>
              @for (i of issues(); track $index) {
                <span
                  class="issue"
                  [class.issue--warn]="i.severity === 'warning'"
                  >{{ i.message }}</span
                >
              }
              @if (d.id) {
                <pp-button variant="ghost" size="sm" (click)="remove(d)"
                  >Delete</pp-button
                >
              }
              <pp-button variant="ghost" size="sm" (click)="close()"
                >Close</pp-button
              >
            </div>

            @if (showPlan()) {
              <div class="plan">
                @if (plan(); as p) {
                  <div class="plan__row">
                    <strong>Start order</strong>
                    @if (p.simulators.length) {
                      <span class="plan__step plan__step--sim"
                        >simulators: {{ simList(p) }}</span
                      >
                      <span class="plan__arrow">→</span>
                    }
                    @for (
                      pp of p.participants;
                      track pp.node_id;
                      let i = $index;
                      let last = $last
                    ) {
                      <span class="plan__step"
                        >{{ i + 1 }}. {{ flowNameOf(pp.flow_id) }}
                        @if (pp.instances > 1) {
                          <span class="muted">×{{ pp.instances }}</span>
                        }
                        @if (pp.port) {
                          <span class="muted"> :{{ pp.port }}+</span>
                        }
                      </span>
                      @if (!last) {
                        <span class="plan__arrow">→</span>
                      }
                    } @empty {
                      <span class="muted">no participants</span>
                    }
                    @if (p.initiators.length) {
                      <span class="plan__arrow">→</span>
                      <span class="plan__step plan__step--init"
                        >drivers: {{ initList(p) }}</span
                      >
                    }
                  </div>
                  @for (w of p.warnings; track $index) {
                    <span class="issue issue--warn">{{ w }}</span>
                  }
                } @else {
                  <span class="muted"
                    >Save the network to compute its launch plan.</span
                  >
                }
              </div>
            }

            <div class="canvas-wrap">
              <svg
                class="canvas"
                [class.canvas--wire]="wireMode()"
                (pointermove)="onDrag($event)"
                (pointerup)="endDrag()"
                (pointerleave)="endDrag()"
                (click)="onCanvasClick()"
              >
                <defs>
                  <marker
                    id="nf-arrow"
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="7"
                    markerHeight="7"
                    orient="auto-start-reverse"
                  >
                    <path
                      d="M 0 0 L 10 5 L 0 10 z"
                      fill="var(--color-brand, #06b6d4)"
                    />
                  </marker>
                </defs>

                @for (e of d.edges; track edgeKey(e)) {
                  <path
                    class="wire"
                    [class.wire--selected]="selectedEdge() === edgeKey(e)"
                    [attr.d]="edgePath(e)"
                    marker-end="url(#nf-arrow)"
                    (click)="selectEdge(e, $event)"
                  />
                }

                @for (n of d.nodes; track n.id) {
                  <g
                    class="node"
                    [class.node--scenario]="n.kind === 'scenario'"
                    [class.node--simulator]="n.kind === 'simulator'"
                    [class.node--group]="n.kind === 'group'"
                    [class.node--selected]="selected()?.id === n.id"
                    [class.node--wiresrc]="wireSource() === n.id"
                    [attr.transform]="
                      'translate(' + pos(n.id).x + ',' + pos(n.id).y + ')'
                    "
                    (pointerdown)="startDrag(n, $event)"
                    (click)="onNodeClick(n, $event)"
                  >
                    <rect [attr.width]="W" [attr.height]="H" rx="12"></rect>
                    <text class="node__kind" x="12" y="18">
                      {{ kindLabel(n) }}
                    </text>
                    <text class="node__label" x="12" y="38">
                      {{ nodeLabel(n) }}
                    </text>
                    @if (
                      n.kind === "participant" && (n.config.instances || 1) > 1
                    ) {
                      <text
                        class="node__mult"
                        [attr.x]="W - 22"
                        y="18"
                        text-anchor="end"
                      >
                        ×{{ n.config.instances }}
                      </text>
                    }
                    @if (orderOf(n.id); as ord) {
                      <circle
                        class="node__order"
                        [attr.cx]="W - 8"
                        cy="8"
                        r="9"
                      ></circle>
                      <text
                        class="node__order-num"
                        [attr.x]="W - 8"
                        y="11.5"
                        text-anchor="middle"
                      >
                        {{ ord }}
                      </text>
                    }
                    @if (liveState(n); as st) {
                      <circle
                        class="node__dot"
                        cx="14"
                        [attr.cy]="H - 9"
                        r="4"
                        [attr.data-state]="st.state"
                      ></circle>
                      <text class="node__live" x="22" [attr.y]="H - 6">
                        {{ st.text }}
                      </text>
                    }
                  </g>
                }
              </svg>

              @if (selected(); as n) {
                <div class="inspector">
                  <div class="inspector__head">
                    <strong>{{ kindLabel(n) }}</strong>
                    <span class="muted mono">{{ n.id }}</span>
                    <span class="spacer"></span>
                    <button
                      class="iconbtn"
                      title="Remove node"
                      (click)="deleteNode(n)"
                    >
                      <pp-icon name="x" [size]="15" />
                    </button>
                  </div>
                  @if (n.kind === "participant") {
                    <label class="fld"
                      ><span>Participant flow</span>
                      <select [(ngModel)]="n.config.flow_id">
                        <option value="">— pick a flow —</option>
                        @for (f of flows.flows(); track f.id) {
                          <option [value]="f.id">{{ f.name }}</option>
                        }
                      </select></label
                    >
                    <div class="grid2">
                      <label class="fld"
                        ><span>Instances</span>
                        <input
                          type="number"
                          min="1"
                          [(ngModel)]="n.config.instances"
                      /></label>
                      <label class="fld"
                        ><span
                          >Base port
                          <small class="muted">(optional)</small></span
                        >
                        <input
                          type="number"
                          [(ngModel)]="n.config.port"
                          placeholder="from connection"
                      /></label>
                    </div>
                  } @else if (n.kind === "scenario") {
                    <label class="fld"
                      ><span>Scenario</span>
                      <select [(ngModel)]="n.config.scenario_id">
                        <option value="">— pick a scenario —</option>
                        @for (s of scenarios(); track s.id) {
                          <option [value]="s.id">{{ s.name }}</option>
                        }
                      </select></label
                    >
                    <label class="fld"
                      ><span>Environment</span>
                      <select [(ngModel)]="n.config.environment">
                        <option [ngValue]="null">— default —</option>
                        @for (e of envs.environments(); track e.name) {
                          <option [ngValue]="e.name">{{ e.name }}</option>
                        }
                      </select></label
                    >
                    <label class="chk">
                      <input type="checkbox" [(ngModel)]="n.config.autostart" />
                      <span>Auto-run when the network starts</span>
                    </label>
                  } @else if (n.kind === "simulator") {
                    <label class="fld"
                      ><span>Saved simulator</span>
                      <select [(ngModel)]="n.config.simulator_id">
                        <option value="">— pick a simulator —</option>
                        @for (s of savedSims(); track s.id) {
                          <option [value]="s.id">{{ s.label }}</option>
                        }
                      </select></label
                    >
                    <p class="muted small-note">
                      Started with the network (if not already running) and
                      stopped with it.
                    </p>
                  } @else {
                    <label class="fld"
                      ><span>Participant group</span>
                      <select [(ngModel)]="n.config.group_id">
                        <option value="">— pick a group —</option>
                        @for (g of groupsSvc.groups(); track g.id) {
                          <option [value]="g.id">{{ g.name }}</option>
                        }
                      </select></label
                    >
                    <p class="muted small-note">
                      A wiring target only — its member connections do the
                      listening.
                    </p>
                  }
                </div>
              }
            </div>
          } @else {
            <div class="empty">
              <pp-icon name="network" [size]="34" />
              <p class="muted">
                Select a network to open its canvas, or create a new one.
              </p>
              <p class="muted small">
                Wiring means “sends traffic to”: switch → issuer starts the
                issuer first. Drop participants and hit
                <strong>Auto-wire</strong> — the edges are derived from what the
                flows already call. No wires = node order is start order.
              </p>
            </div>
          }
        </section>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        color: var(--color-text);
      }
      .nf__body {
        display: grid;
        grid-template-columns: minmax(240px, 300px) 1fr;
        gap: 1rem;
        padding: 0 1.25rem 1.25rem;
      }
      .nf__list {
        border: 1px solid var(--color-border);
        border-radius: 12px;
        background: var(--color-card);
        overflow: auto;
        max-height: 78vh;
      }
      .nf__list-head {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 0.6rem 0.75rem;
        border-bottom: 1px solid var(--color-border);
        font-size: 0.8rem;
        color: var(--color-muted);
      }
      .spacer {
        flex: 1;
      }
      .row {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        width: 100%;
        padding: 0.6rem 0.75rem;
        border: none;
        border-bottom: 1px solid var(--color-border);
        background: none;
        color: inherit;
        cursor: pointer;
        text-align: left;
      }
      .row.active,
      .row:hover {
        background: var(--color-node);
      }
      .row__main {
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }
      .muted {
        color: var(--color-muted);
      }
      .small {
        font-size: 0.8rem;
        max-width: 420px;
        text-align: center;
      }
      .mono {
        font-family: var(--font-mono, monospace);
        font-size: 0.75rem;
      }
      .pad {
        padding: 1rem;
      }
      .nf__onboard {
        display: flex;
        flex-direction: column;
        gap: 8px;
        align-items: flex-start;
        padding: 1rem;
      }
      .nf__onboard .small {
        font-size: 0.8rem;
        margin: 0;
      }
      .iconbtn {
        background: none;
        border: none;
        color: var(--color-muted);
        cursor: pointer;
        padding: 4px;
        border-radius: 6px;
        display: inline-flex;
      }
      .iconbtn:hover {
        background: var(--color-node);
        color: var(--color-text);
      }
      .bar {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin-bottom: 0.5rem;
      }
      .bar--tools {
        flex-wrap: wrap;
      }
      .bar__name {
        font-weight: 600;
        min-width: 200px;
      }
      .bar__desc {
        flex: 1;
        min-width: 120px;
      }
      .bar input {
        padding: 0.45rem 0.6rem;
        border: 1px solid var(--color-border);
        border-radius: 8px;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .issue {
        font-size: 0.75rem;
        padding: 3px 8px;
        border-radius: 6px;
        background: color-mix(in srgb, #ef4444 14%, transparent);
        color: #ef4444;
      }
      .issue--warn {
        background: color-mix(in srgb, #f59e0b 14%, transparent);
        color: #b45309;
      }
      .canvas-wrap {
        position: relative;
      }
      .canvas {
        width: 100%;
        height: 62vh;
        min-height: 420px;
        display: block;
        border: 1px solid var(--color-border);
        border-radius: 12px;
        background:
          radial-gradient(
              circle,
              color-mix(in srgb, var(--color-border) 55%, transparent) 1px,
              transparent 1px
            )
            0 0 / 22px 22px,
          var(--color-bg);
      }
      .canvas--wire {
        cursor: crosshair;
      }
      .node {
        cursor: grab;
      }
      .node rect {
        fill: var(--color-card);
        stroke: var(--color-border);
        stroke-width: 1.5;
      }
      .node--scenario rect {
        stroke: #f59e0b;
        stroke-dasharray: none;
      }
      .node--simulator rect {
        stroke: #8b5cf6;
      }
      .node--group rect {
        stroke: #10b981;
        stroke-dasharray: 4 3;
      }
      .node--simulator .node__kind {
        fill: #8b5cf6 !important;
      }
      .node--group .node__kind {
        fill: #10b981 !important;
      }
      .small-note {
        font-size: 0.75rem;
        margin: 0;
      }
      .node--selected rect {
        stroke: var(--color-brand, #06b6d4);
        stroke-width: 2.5;
      }
      .node--wiresrc rect {
        stroke: var(--color-brand, #06b6d4);
        stroke-dasharray: 5 3;
        stroke-width: 2.5;
      }
      .node text {
        fill: var(--color-text);
        font-size: 13px;
        pointer-events: none;
      }
      .node__kind {
        fill: var(--color-muted) !important;
        font-size: 10px !important;
        text-transform: uppercase;
        letter-spacing: 0.06em;
      }
      .node--scenario .node__kind {
        fill: #f59e0b !important;
      }
      .node__mult {
        fill: var(--color-muted) !important;
        font-size: 11px !important;
      }
      .node__order {
        fill: var(--color-brand, #06b6d4);
        stroke: var(--color-card);
        stroke-width: 1.5;
      }
      .node__order-num {
        fill: #fff !important;
        font-size: 10px !important;
        font-weight: 700;
      }
      .node__dot[data-state="up"] {
        fill: #10b981;
      }
      .node__dot[data-state="part"] {
        fill: #f59e0b;
      }
      .node__dot[data-state="down"] {
        fill: #ef4444;
      }
      .node__live {
        fill: var(--color-muted) !important;
        font-size: 9px !important;
        font-family: var(--font-mono, monospace);
      }
      .plan {
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 0.55rem 0.75rem;
        margin-bottom: 0.5rem;
        border: 1px dashed var(--color-border);
        border-radius: 10px;
        background: var(--color-card);
        font-size: 0.8rem;
      }
      .plan__row {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
      }
      .plan__step {
        padding: 2px 8px;
        border: 1px solid var(--color-border);
        border-radius: 999px;
        background: var(--color-bg);
      }
      .plan__step--sim {
        border-color: #8b5cf6;
      }
      .plan__step--init {
        border-color: #f59e0b;
      }
      .plan__arrow {
        color: var(--color-muted);
      }
      .wire {
        fill: none;
        stroke: var(--color-brand, #06b6d4);
        stroke-width: 2;
        stroke-dasharray: 7 5;
        opacity: 0.85;
        cursor: pointer;
      }
      .wire--selected {
        stroke-width: 3.5;
        stroke-dasharray: none;
        opacity: 1;
      }
      .inspector {
        position: absolute;
        top: 12px;
        right: 12px;
        width: 260px;
        padding: 0.8rem;
        border: 1px solid var(--color-border);
        border-radius: 12px;
        background: var(--color-card);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.12);
        display: flex;
        flex-direction: column;
        gap: 0.6rem;
      }
      .inspector__head {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .fld {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 0.82rem;
      }
      .fld input,
      .fld select {
        padding: 0.4rem 0.55rem;
        border: 1px solid var(--color-border);
        border-radius: 8px;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .grid2 {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0.5rem;
      }
      .chk {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 0.82rem;
      }
      .empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 0.5rem;
        min-height: 320px;
        color: var(--color-muted);
        border: 1px dashed var(--color-border);
        border-radius: 12px;
      }
    `,
  ],
})
export class NetworkFlowsComponent implements OnInit, OnDestroy {
  readonly svc = inject(NetworkFlowsService);
  readonly flows = inject(ParticipantsService);
  readonly envs = inject(EnvironmentsService);
  readonly groupsSvc = inject(GroupsService);
  readonly theme = inject(ThemeService);
  private readonly ui = inject(UiService);
  private readonly scenarioApi = inject(ScenarioApiService);
  private readonly runApi = inject(RunApiService);
  private readonly route = inject(ActivatedRoute);

  readonly W = NODE_W;
  readonly H = NODE_H;

  /** Saved scenarios, for initiator pickers. */
  readonly scenarios = signal<ScenarioSummary[]>([]);
  /** Saved simulators, for simulator-node pickers. */
  readonly savedSims = signal<SavedSimulator[]>([]);

  readonly draft = signal<NetDraft | null>(null);
  readonly selected = signal<NetworkNode | null>(null);
  readonly selectedEdge = signal<string | null>(null);
  readonly issues = signal<NetworkFlowIssue[]>([]);
  readonly wireMode = signal(false);
  readonly wireSource = signal<string | null>(null);
  /** Launch plan of the open (saved) network — start order, sims, drivers. */
  readonly plan = signal<NetworkFlowPlan | null>(null);
  readonly showPlan = signal(false);
  /** One-click showcase install in flight (empty-state onboarding). */
  readonly installing = signal(false);

  readonly running = computed(() => {
    const d = this.draft();
    return d?.id ? this.svc.runFor(d.id) : undefined;
  });

  private drag: { id: string; dx: number; dy: number } | null = null;
  /** Poll live run state while the canvas is open (drives the node overlay);
   *  skips ticks while the tab is hidden. */
  private readonly poll = new PagePoll(5000, () => {
    if (this.draft()?.id) this.svc.reloadRuns();
  });

  ngOnInit(): void {
    this.refresh();
    this.flows.reloadFlows();
    this.envs.reload();
    this.scenarioApi.list().subscribe({
      next: (s) => this.scenarios.set(s),
      error: () => this.scenarios.set([]),
    });
    this.groupsSvc.reload();
    this.runApi.savedSimulators().subscribe({
      next: (s) => this.savedSims.set(s),
      error: () => this.savedSims.set([]),
    });
    this.poll.start();
    // deep link from the Topology Map ("Canvas" button): ?id=<network id>
    const deepLink = this.route.snapshot.queryParamMap.get("id");
    if (deepLink) this.open(deepLink);
  }

  ngOnDestroy(): void {
    this.poll.stop();
  }

  refresh(): void {
    this.svc.reload();
  }

  // -- open / create ---------------------------------------------------------

  newFlow(): void {
    this.resetSelection();
    this.draft.set({
      name: "",
      description: "",
      nodes: [],
      edges: [],
      layout: {},
    });
  }

  /** Empty-state onboarding: build the bundled showcase network and open it. */
  loadShowcase(): void {
    this.installing.set(true);
    this.svc.installShowcase(false).subscribe({
      next: (res) => {
        this.installing.set(false);
        this.ui.toast(
          "success",
          "Showcase network loaded — press Start to run it.",
        );
        this.open(res.network_flow_id);
      },
      error: (e) => {
        this.installing.set(false);
        this.ui.toast(
          "error",
          e?.error?.detail ?? "Could not load the showcase.",
        );
      },
    });
  }

  open(id: string): void {
    this.resetSelection();
    this.svc.get(id).subscribe({
      next: (full) => {
        this.draft.set({
          id: full.id,
          name: full.name,
          description: full.description ?? "",
          nodes: (full.nodes ?? []).map((n) => ({
            id: n.id,
            kind: n.kind,
            config: { ...(n.config ?? {}) },
          })),
          edges: (full.edges ?? []).map((e) => ({
            source: e.source,
            target: e.target,
          })),
          layout: { ...(full.layout ?? {}) },
        });
        this.autoLayoutMissing();
        this.loadPlan(id);
      },
      error: () => this.ui.toast("error", "Could not load the network."),
    });
  }

  private loadPlan(id: string): void {
    this.svc.plan(id).subscribe({
      next: (p) => this.plan.set(p),
      error: () => this.plan.set(null),
    });
  }

  togglePlan(): void {
    this.showPlan.update((v) => !v);
  }

  close(): void {
    this.resetSelection();
    this.draft.set(null);
  }

  private resetSelection(): void {
    this.selected.set(null);
    this.selectedEdge.set(null);
    this.wireMode.set(false);
    this.wireSource.set(null);
    this.issues.set([]);
    this.plan.set(null);
  }

  // -- plan / live overlay -----------------------------------------------------

  flowNameOf(flowId: string): string {
    return this.flows.flows().find((f) => f.id === flowId)?.name ?? flowId;
  }

  simList(p: NetworkFlowPlan): string {
    return p.simulators
      .map(
        (s) =>
          this.savedSims().find((x) => x.id === s.simulator_id)?.label ??
          s.simulator_id,
      )
      .join(", ");
  }

  initList(p: NetworkFlowPlan): string {
    return p.initiators
      .map(
        (i) =>
          (this.scenarios().find((s) => s.id === i.scenario_id)?.name ??
            i.scenario_id) + (i.autostart ? "" : " (manual)"),
      )
      .join(", ");
  }

  /** 1-based start position of a participant node, from the saved plan. */
  orderOf(nodeId: string): string | null {
    const p = this.plan();
    if (!p) return null;
    const idx = p.participants.findIndex((x) => x.node_id === nodeId);
    return idx >= 0 ? String(idx + 1) : null;
  }

  /** Live status of a node while a run of this network exists. */
  liveState(n: NetworkNode): { state: string; text: string } | null {
    const run = this.running();
    if (!run) return null;
    if (n.kind !== "participant" && n.kind !== "simulator") return null;
    const key =
      n.kind === "participant" ? n.config.flow_id : n.config.simulator_id;
    if (!key) return null;
    const mine = (run.endpoints ?? []).filter((e) => e.flow_id === key);
    const expected =
      n.kind === "participant"
        ? Math.max(1, Number(n.config.instances) || 1)
        : 1;
    const state =
      mine.length >= expected ? "up" : mine.length ? "part" : "down";
    const ports = mine
      .slice(0, 3)
      .map((e) => ":" + (e.endpoint.split(":").pop() ?? "?"))
      .join(" ");
    return {
      state,
      text: mine.length ? `${mine.length}/${expected} ${ports}` : "not running",
    };
  }

  autoWire(): void {
    const d = this.draft();
    if (!d) return;
    this.svc.inferWiring(this.body(d)).subscribe({
      next: (res) => {
        const have = new Set(d.edges.map((e) => this.edgeKey(e)));
        const fresh = res.edges.filter(
          (e) => !have.has(this.edgeKey(e)) && e.source !== e.target,
        );
        d.edges = [...d.edges, ...fresh];
        this.draft.set({ ...d });
        if (fresh.length) {
          this.ui.toast(
            "success",
            `${fresh.length} wire(s) added from the flows' own targets.`,
          );
        } else {
          this.ui.toast(
            "success",
            res.notes[0] ??
              "Nothing to add — the wiring already matches the flows.",
          );
        }
      },
      error: (e) =>
        this.ui.toast("error", e?.error?.detail ?? "Could not infer wiring."),
    });
  }

  // -- nodes ------------------------------------------------------------------

  kindLabel(n: NetworkNode): string {
    return n.kind === "participant"
      ? "participant"
      : n.kind === "scenario"
        ? "initiator"
        : n.kind === "simulator"
          ? "simulator"
          : "group";
  }

  nodeLabel(n: NetworkNode): string {
    if (n.kind === "participant") {
      const f = this.flows.flows().find((x) => x.id === n.config.flow_id);
      return f?.name || n.config.flow_id || "(pick a flow)";
    }
    if (n.kind === "scenario") {
      const s = this.scenarios().find((x) => x.id === n.config.scenario_id);
      return s?.name || n.config.scenario_id || "(pick a scenario)";
    }
    if (n.kind === "simulator") {
      const s = this.savedSims().find((x) => x.id === n.config.simulator_id);
      return s?.label || n.config.simulator_id || "(pick a simulator)";
    }
    const g = this.groupsSvc.groups().find((x) => x.id === n.config.group_id);
    return g?.name || n.config.group_id || "(pick a group)";
  }

  pos(id: string): { x: number; y: number } {
    return this.draft()?.layout[id] ?? { x: 40, y: 40 };
  }

  private uniqueId(base: string): string {
    const d = this.draft();
    if (!d) return base;
    const taken = new Set(d.nodes.map((n) => n.id));
    if (!taken.has(base)) return base;
    let i = 2;
    while (taken.has(`${base}-${i}`)) i += 1;
    return `${base}-${i}`;
  }

  addParticipant(): void {
    const d = this.draft();
    if (!d) return;
    const id = this.uniqueId(`participant-${d.nodes.length + 1}`);
    const n: NetworkNode = {
      id,
      kind: "participant",
      config: { flow_id: "", instances: 1 },
    };
    d.nodes.push(n);
    d.layout[id] = this.nextSpot();
    this.selected.set(n);
  }

  addInitiator(): void {
    const d = this.draft();
    if (!d) return;
    const id = this.uniqueId(`initiator-${d.nodes.length + 1}`);
    const n: NetworkNode = {
      id,
      kind: "scenario",
      config: { scenario_id: "", environment: null, autostart: true },
    };
    d.nodes.push(n);
    d.layout[id] = this.nextSpot();
    this.selected.set(n);
  }

  addSimulator(): void {
    const d = this.draft();
    if (!d) return;
    const id = this.uniqueId(`simulator-${d.nodes.length + 1}`);
    const n: NetworkNode = {
      id,
      kind: "simulator",
      config: { simulator_id: "" },
    };
    d.nodes.push(n);
    d.layout[id] = this.nextSpot();
    this.selected.set(n);
  }

  addGroup(): void {
    const d = this.draft();
    if (!d) return;
    const id = this.uniqueId(`group-${d.nodes.length + 1}`);
    const n: NetworkNode = { id, kind: "group", config: { group_id: "" } };
    d.nodes.push(n);
    d.layout[id] = this.nextSpot();
    this.selected.set(n);
  }

  deleteNode(n: NetworkNode): void {
    const d = this.draft();
    if (!d) return;
    d.nodes = d.nodes.filter((x) => x.id !== n.id);
    d.edges = d.edges.filter((e) => e.source !== n.id && e.target !== n.id);
    delete d.layout[n.id];
    this.selected.set(null);
    this.draft.set({ ...d });
  }

  private nextSpot(): { x: number; y: number } {
    const d = this.draft();
    const i = d ? d.nodes.length - 1 : 0;
    return {
      x: 60 + (i % 4) * (NODE_W + 60),
      y: 60 + Math.floor(i / 4) * (NODE_H + 70),
    };
  }

  /** Place nodes that have no saved position (e.g. migrated topologies). */
  private autoLayoutMissing(): void {
    const d = this.draft();
    if (!d) return;
    d.nodes.forEach((n, i) => {
      if (!d.layout[n.id]) {
        d.layout[n.id] = {
          x: 60 + (i % 4) * (NODE_W + 60),
          y: 60 + Math.floor(i / 4) * (NODE_H + 70),
        };
      }
    });
  }

  // -- selection / wiring -------------------------------------------------------

  onNodeClick(n: NetworkNode, ev: Event): void {
    ev.stopPropagation();
    if (this.wireMode()) {
      const src = this.wireSource();
      if (!src) {
        this.wireSource.set(n.id);
        return;
      }
      this.finishWire(src, n);
      return;
    }
    this.selectedEdge.set(null);
    this.selected.set(n);
  }

  onCanvasClick(): void {
    this.selected.set(null);
    this.selectedEdge.set(null);
    if (this.wireMode()) this.wireSource.set(null);
  }

  toggleWireMode(): void {
    this.wireMode.update((w) => !w);
    this.wireSource.set(null);
    this.selected.set(null);
    this.selectedEdge.set(null);
  }

  private finishWire(srcId: string, target: NetworkNode): void {
    const d = this.draft();
    this.wireSource.set(null);
    this.wireMode.set(false);
    if (!d || srcId === target.id) return;
    if (target.kind === "scenario") {
      this.ui.toast(
        "error",
        "Initiators drive traffic — nothing wires into them.",
      );
      return;
    }
    const src = d.nodes.find((x) => x.id === srcId);
    if (src?.kind === "simulator") {
      this.ui.toast(
        "error",
        "Simulators only receive traffic — wire into them, not out.",
      );
      return;
    }
    if (src?.kind === "group" && target.kind !== "participant") {
      this.ui.toast(
        "error",
        "A group only fans out to participants (the listeners its members reach).",
      );
      return;
    }
    if (d.edges.some((e) => e.source === srcId && e.target === target.id))
      return;
    d.edges.push({ source: srcId, target: target.id });
    this.draft.set({ ...d });
  }

  edgeKey(e: NetworkEdge): string {
    return `${e.source}->${e.target}`;
  }

  selectEdge(e: NetworkEdge, ev: Event): void {
    ev.stopPropagation();
    this.selected.set(null);
    this.selectedEdge.set(this.edgeKey(e));
  }

  deleteSelectedEdge(): void {
    const d = this.draft();
    const key = this.selectedEdge();
    if (!d || !key) return;
    d.edges = d.edges.filter((e) => this.edgeKey(e) !== key);
    this.selectedEdge.set(null);
    this.draft.set({ ...d });
  }

  edgePath(e: NetworkEdge): string {
    const a = this.pos(e.source);
    const b = this.pos(e.target);
    const x1 = a.x + NODE_W / 2;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x + NODE_W / 2;
    const y2 = b.y + NODE_H / 2;
    // trim the line to the boxes' borders so the arrowhead lands on the edge
    const dx = x2 - x1;
    const dy = y2 - y1;
    const len = Math.max(1, Math.hypot(dx, dy));
    const trimA = this.borderTrim(dx, dy);
    const trimB = this.borderTrim(-dx, -dy);
    const sx = x1 + (dx / len) * trimA;
    const sy = y1 + (dy / len) * trimA;
    const tx = x2 - (dx / len) * trimB;
    const ty = y2 - (dy / len) * trimB;
    const mx = (sx + tx) / 2;
    return `M ${sx} ${sy} C ${mx} ${sy}, ${mx} ${ty}, ${tx} ${ty}`;
  }

  /** Distance from a box center to its border along direction (dx, dy). */
  private borderTrim(dx: number, dy: number): number {
    const ax = Math.abs(dx);
    const ay = Math.abs(dy);
    if (ax < 1e-6 && ay < 1e-6) return 0;
    const sx = ax > 1e-6 ? NODE_W / 2 / ax : Infinity;
    const sy = ay > 1e-6 ? NODE_H / 2 / ay : Infinity;
    return Math.min(sx, sy) * Math.hypot(dx, dy);
  }

  // -- drag -------------------------------------------------------------------

  startDrag(n: NetworkNode, ev: PointerEvent): void {
    if (this.wireMode()) return;
    const p = this.pos(n.id);
    this.drag = { id: n.id, dx: ev.clientX - p.x, dy: ev.clientY - p.y };
  }

  onDrag(ev: PointerEvent): void {
    const d = this.draft();
    if (!d || !this.drag) return;
    d.layout[this.drag.id] = {
      x: Math.max(0, ev.clientX - this.drag.dx),
      y: Math.max(0, ev.clientY - this.drag.dy),
    };
    this.draft.set({ ...d });
  }

  endDrag(): void {
    this.drag = null;
  }

  // -- save / run ---------------------------------------------------------------

  private body(d: NetDraft): Partial<NetworkFlow> {
    return {
      name: d.name.trim(),
      description: d.description,
      nodes: d.nodes.map((n) => ({
        id: n.id,
        kind: n.kind,
        config: this.cleanConfig(n),
      })),
      edges: d.edges.map((e) => ({ source: e.source, target: e.target })),
      layout: d.layout,
    };
  }

  private cleanConfig(n: NetworkNode): NetworkNode["config"] {
    if (n.kind === "participant") {
      const c: NetworkNode["config"] = {
        flow_id: (n.config.flow_id || "").trim(),
        instances: Math.max(1, Number(n.config.instances) || 1),
      };
      if (n.config.port) c.port = Number(n.config.port);
      return c;
    }
    if (n.kind === "scenario") {
      return {
        scenario_id: (n.config.scenario_id || "").trim(),
        environment: n.config.environment || null,
        autostart: n.config.autostart !== false,
      };
    }
    if (n.kind === "simulator") {
      return { simulator_id: (n.config.simulator_id || "").trim() };
    }
    return { group_id: (n.config.group_id || "").trim() };
  }

  save(): void {
    const d = this.draft();
    if (!d) return;
    if (!d.name.trim()) {
      this.ui.toast("error", "Give the network flow a name.");
      return;
    }
    const body = this.body(d);
    const op = d.id ? this.svc.save(d.id, body) : this.svc.create(body);
    op.subscribe({
      next: (f) => {
        this.ui.toast("success", `Saved “${f.name}”`);
        this.draft.update((cur) => (cur ? { ...cur, id: f.id } : cur));
        this.loadPlan(f.id);
        this.svc.validate(body).subscribe({
          next: (r) =>
            this.issues.set(r.issues.filter((i) => i.severity === "warning")),
          error: () => this.issues.set([]),
        });
      },
      error: (e) => this.ui.toast("error", e?.error?.detail ?? "Save failed."),
    });
  }

  async remove(d: NetDraft): Promise<void> {
    if (!d.id) return;
    if (
      !(await this.ui.confirm(`Delete network “${d.name}”?`, { danger: true }))
    )
      return;
    this.svc.remove(d.id).subscribe({
      next: () => {
        this.ui.toast("success", "Deleted.");
        this.close();
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }

  start(): void {
    const d = this.draft();
    if (!d?.id) {
      this.ui.toast("error", "Save the network first.");
      return;
    }
    this.svc.start(d.id).subscribe({
      next: () => this.ui.toast("success", "Network started."),
      error: (e) =>
        this.ui.toast(
          "error",
          e?.error?.detail ?? "Could not start the network.",
        ),
    });
  }

  stop(runId: string): void {
    this.svc.stopRun(runId).subscribe({
      next: () => this.ui.toast("success", "Stopped."),
      error: () => this.ui.toast("error", "Could not stop."),
    });
  }
}
