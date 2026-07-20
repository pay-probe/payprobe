import {
  Component,
  ElementRef,
  OnInit,
  OnDestroy,
  ViewChild,
  inject,
  signal,
  computed,
  effect,
  HostListener,
  ChangeDetectionStrategy,
} from "@angular/core";

import {
  GraphEdge,
  GraphNode,
  NetworkGraph,
  RunApiService,
} from "../run-monitor/run-api.service";
import { ActivatedRoute, Router } from "@angular/router";

import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { NetworkGraphStreamService } from "../run-monitor/network-graph-stream.service";
import { ChronoscopeRecorderService } from "./chronoscope/chronoscope-recorder.service";
import { declCount } from "./chronoscope/chronoscope.model";
import { Tm2NodeDrawerComponent } from "./tm2-node-drawer.component";
import { MapSwitcherComponent } from "./map-switcher.component";
import { CaptureChipComponent } from "./capture-chip.component";
import { computeMapLayout, NODE_W, NODE_H } from "./tm2-layout";

interface PNode extends GraphNode {
  x: number;
  y: number;
  lane: number;
  rate: number;
  active: boolean;
  /** approve/decline composition (from by_rc) for the node bar. */
  rcTotal: number;
  okW: number;
  declW: number;
  /** brand throughput bar width when the node has no rc breakdown. */
  barW: number;
  health: "ok" | "warn" | "down" | "idle";
}
interface PEdge extends GraphEdge {
  d: string;
  active: boolean;
  midX: number;
  midY: number;
}
/** A topology container box placed around its member nodes. */
interface PContainer {
  id: string;
  label: string;
  networkFlowId?: string;
  x: number;
  y: number;
  w: number;
  h: number;
}
interface FeedEntry {
  key: number;
  /** Node id — a feed row deep-links to its node (focus + drawer). */
  id: string;
  time: string;
  label: string;
  kind: string;
  sub: string; // endpoint / protocol:port
  status: string;
  delta: number;
  rate: number;
  total: number; // cumulative messages received
  tone: "ok" | "warn" | "info";
}
interface TLSample {
  tps: number;
  decl: number;
}

const BAR_W = 166; // node composition/throughput bar track width
const TL_W = 600;
const TL_H = 56;

/**
 * Topology Map 2 — a single live ops board for the whole simulated network.
 * Lanes (drivers → participants → downstream sims/hosts), request/response
 * packets animating on edges that carried traffic since the last poll, node
 * hit-pulse + approve/decline composition bars + health alert rings, a
 * throughput trend timeline, at-a-glance KPIs, click-to-focus on a node's
 * paths, and a rolling activity feed. Driven by GET /network-graph (2s poll).
 */
@Component({
  selector: "app-topology-map2",
  standalone: true,
  imports: [
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    Tm2NodeDrawerComponent,
    MapSwitcherComponent,
    CaptureChipComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tm2">
      <!-- health metrics -->
      <div class="kpis">
        <div class="kpi">
          <span class="kpi__k">Throughput</span>
          <span class="kpi__v"
            >{{ totalRate() }}<span class="kpi__u">msg/s</span></span
          >
        </div>
        <div class="kpi">
          <span class="kpi__k">Approved</span>
          <span class="kpi__v" [class.kpi__v--ok]="approval() !== null">
            {{ approval() === null ? "—" : approval() + "%" }}
          </span>
        </div>
        <div class="kpi">
          <span class="kpi__k">Declined</span>
          <span class="kpi__v" [class.kpi__v--bad]="declined() > 0">{{
            declined()
          }}</span>
        </div>
        <div class="kpi">
          <span class="kpi__k">Nodes up</span>
          <span class="kpi__v"
            >{{ nodesUp()
            }}<span class="kpi__of">/ {{ listenerCount() }}</span></span
          >
        </div>
        <div class="kpi">
          <span class="kpi__k">Active peers</span>
          <span class="kpi__v">{{ activePeers() }}</span>
        </div>
      </div>

      <!-- control bar -->
      <div class="bar">
        <app-map-switcher />
        <span
          class="live"
          [class.live--paused]="paused()"
          [class.live--stale]="stale()"
        >
          <span class="dot" [class.pulse]="!paused() && !stale()"></span>
          {{ stale() ? "Stale" : paused() ? "Paused" : "Live" }}
        </span>
        <span class="muted sm">{{
          paused()
            ? "updates paused"
            : stream.transport() === "sse"
              ? "streaming"
              : "every 2s"
        }}</span>
        <app-capture-chip />
        @if (selected()) {
          <span class="focus">
            <pp-icon name="flow" [size]="13" /> focused: {{ selectedLabel() }}
            <button
              class="focus__x"
              (click)="clearFocus()"
              aria-label="Clear focus"
            >
              <pp-icon name="x" [size]="12" />
            </button>
          </span>
        }
        <span class="spacer"></span>
        <div
          class="zoom"
          title="Ctrl+scroll zooms at the cursor · drag empty canvas to pan"
        >
          <button class="zbtn" (click)="zoomBy(-0.15)" aria-label="Zoom out">
            <pp-icon name="minus" [size]="14" />
          </button>
          <span class="zval">{{ zoomPct() }}%</span>
          <button class="zbtn" (click)="zoomBy(0.15)" aria-label="Zoom in">
            <pp-icon name="plus" [size]="14" />
          </button>
          <button class="zbtn" (click)="fitToView()">Fit</button>
          <button class="zbtn" (click)="resetZoom()">100%</button>
        </div>
        @if (hasManualLayout()) {
          <pp-button
            variant="ghost"
            size="sm"
            icon="undo"
            (click)="resetLayout()"
            >Reset layout</pp-button
          >
        }
        <pp-button
          variant="ghost"
          size="sm"
          [icon]="paused() ? 'play' : 'clock'"
          (click)="togglePause()"
        >
          {{ paused() ? "Resume" : "Pause" }}
        </pp-button>
        <pp-button
          variant="secondary"
          size="sm"
          icon="activity"
          (click)="refresh()"
          >Refresh</pp-button
        >
      </div>

      @if (stale()) {
        <div class="stale" role="alert">
          <pp-icon name="alert-circle" [size]="15" />
          <span>
            <strong>Lost contact with the orchestrator</strong> — retrying every
            2s.
            @if (pollHealth.sinceOk() !== null) {
              The board shows data from {{ pollHealth.sinceOk() }}s ago.
            } @else {
              Nothing has been received yet.
            }
          </span>
        </div>
      }

      <div class="split" [class.split--drawer]="!!selectedNode()">
        <div class="boardwrap" [class.boardwrap--stale]="stale()">
          @if (!graph()) {
            <div class="state"><span class="muted">Loading network…</span></div>
          } @else if (!layout().nodes.length) {
            <div class="state state--empty">
              <span class="state__icon"
                ><pp-icon name="flow" [size]="28"
              /></span>
              <h3>Nothing running yet</h3>
              <p class="muted">
                Start a topology, a participant flow or a simulator and it
                appears here — with live traffic flowing between the nodes.
              </p>
            </div>
          } @else {
            <div
              class="canvas"
              #canvasBox
              [class.canvas--panning]="panning()"
              (wheel)="onWheel($event)"
              (pointerdown)="onCanvasDown($event)"
            >
              <svg
                [attr.viewBox]="'0 0 ' + layout().width + ' ' + layout().height"
                [style.width.px]="layout().width * zoom()"
                [style.height.px]="layout().height * zoom()"
                xmlns="http://www.w3.org/2000/svg"
              >
                <defs>
                  <marker
                    id="tm2-arrow"
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="7"
                    markerHeight="7"
                    orient="auto-start-reverse"
                  >
                    <path d="M0,0 L10,5 L0,10 z" class="arrowhead" />
                  </marker>
                </defs>

                @for (c of layout().containers; track c.id) {
                  <g class="cont">
                    <rect
                      [attr.x]="c.x"
                      [attr.y]="c.y"
                      [attr.width]="c.w"
                      [attr.height]="c.h"
                      rx="16"
                      class="cont__box"
                    />
                    <text
                      [attr.x]="c.x + 14"
                      [attr.y]="c.y + 17"
                      class="cont__lbl"
                      [class.cont__lbl--link]="!!c.networkFlowId"
                      (click)="openCanvas(c.networkFlowId)"
                    >
                      {{ c.label }}
                      @if (c.networkFlowId) {
                        <tspan class="cont__edit">· canvas ↗</tspan>
                      }
                    </text>
                  </g>
                }

                @for (e of layout().edges; track e.id) {
                  <g
                    class="edge"
                    [class.edge--active]="e.active"
                    [attr.data-kind]="e.kind"
                    [attr.opacity]="edgeOpacity(e)"
                  >
                    <path
                      [attr.id]="pathId(e)"
                      [attr.d]="e.d"
                      class="edge__wire"
                      marker-end="url(#tm2-arrow)"
                    />
                    @if (e.active && !stale() && !docHidden()) {
                      <path [attr.d]="e.d" class="edge__flow" />
                      <circle r="3.6" class="edge__packet">
                        <animateMotion
                          [attr.dur]="packetDur(e)"
                          repeatCount="indefinite"
                          rotate="auto"
                        >
                          <mpath [attr.href]="'#' + pathId(e)" />
                        </animateMotion>
                      </circle>
                      <circle r="3.6" class="edge__packet">
                        <animateMotion
                          [attr.dur]="packetDur(e)"
                          begin="-0.55s"
                          repeatCount="indefinite"
                          rotate="auto"
                        >
                          <mpath [attr.href]="'#' + pathId(e)" />
                        </animateMotion>
                      </circle>
                    }
                    @if (
                      e.label &&
                      e.kind !== "drive" &&
                      e.kind !== "client" &&
                      e.kind !== "init"
                    ) {
                      <text
                        [attr.x]="e.midX"
                        [attr.y]="e.midY - 6"
                        text-anchor="middle"
                        class="edge__lbl"
                      >
                        {{ e.label }}
                      </text>
                    }
                  </g>
                }

                @for (n of layout().nodes; track n.id) {
                  <g
                    class="node"
                    [attr.data-kind]="n.kind"
                    [attr.data-status]="n.status"
                    [attr.data-health]="n.health"
                    [attr.transform]="'translate(' + n.x + ',' + n.y + ')'"
                    [attr.opacity]="nodeOpacity(n)"
                    [class.node--active]="n.active"
                    [class.node--sel]="selected() === n.id"
                    (pointerdown)="onNodeDown($event, n)"
                  >
                    @if (n.health === "warn") {
                      <rect
                        x="-4"
                        y="-4"
                        width="220"
                        height="78"
                        rx="14"
                        class="node__ring"
                      />
                    }
                    <rect width="212" height="70" rx="12" class="node__box" />
                    <rect width="4" height="70" rx="2" class="node__accent" />
                    <circle
                      cx="20"
                      cy="20"
                      r="5"
                      class="node__status"
                      [class.pulse]="n.active && !stale() && !docHidden()"
                    />
                    <foreignObject x="8" y="40" width="22" height="22">
                      <span
                        xmlns="http://www.w3.org/1999/xhtml"
                        class="node__ico"
                        ><pp-icon [name]="kindIcon(n.kind)" [size]="16"
                      /></span>
                    </foreignObject>
                    <text x="34" y="24" class="node__label">
                      {{ nodeLabel(n) }}
                    </text>
                    <text x="34" y="40" class="node__sub">
                      {{ nodeSub(n) }}
                    </text>
                    @if (n.kind === "participant" || n.kind === "simulator") {
                      <rect
                        x="34"
                        y="52"
                        width="166"
                        height="4"
                        rx="2"
                        class="node__track"
                      />
                      @if (n.rcTotal > 0) {
                        <rect
                          x="34"
                          y="52"
                          [attr.width]="n.okW"
                          height="4"
                          rx="2"
                          class="node__ok"
                        />
                        <rect
                          [attr.x]="34 + n.okW"
                          y="52"
                          [attr.width]="n.declW"
                          height="4"
                          rx="2"
                          class="node__decl"
                        />
                      } @else if (n.barW > 0) {
                        <rect
                          x="34"
                          y="52"
                          [attr.width]="n.barW"
                          height="4"
                          rx="2"
                          class="node__bar"
                        />
                      }
                    }
                    @if (n.status === "unresolved") {
                      <text
                        x="200"
                        y="24"
                        text-anchor="end"
                        class="node__ext node__ext--bad"
                      >
                        unresolved
                      </text>
                    } @else if (n.kind === "group") {
                      <text
                        x="200"
                        y="24"
                        text-anchor="end"
                        class="node__ext node__ext--fleet"
                      >
                        fleet
                      </text>
                    } @else if (nodeStat(n)) {
                      <text
                        x="200"
                        y="24"
                        text-anchor="end"
                        class="node__stat"
                        [class.node__stat--hot]="n.active"
                      >
                        {{ nodeStat(n) }}
                      </text>
                    }
                  </g>
                }
              </svg>
            </div>

            <!-- throughput trend -->
            <div class="trend">
              <div class="trend__head">
                <span
                  >Throughput{{
                    trendSpan() ? " · last " + trendSpan() : ""
                  }}</span
                >
                <span class="trend__leg"
                  ><i class="lg--tps"></i> msg/s
                  <i class="lg--decl"></i> declines</span
                >
              </div>
              <svg
                [attr.viewBox]="'0 0 ' + TL_W + ' ' + TL_H"
                preserveAspectRatio="none"
                class="trend__svg"
              >
                <polygon [attr.points]="tlArea()" class="trend__area" />
                <polyline [attr.points]="tlLine()" class="trend__line" />
                <polyline [attr.points]="tlDecl()" class="trend__decl" />
              </svg>
            </div>
          }
        </div>

        @if (selectedNode(); as sn) {
          <app-tm2-node-drawer
            [node]="sn"
            [rate]="sn.rate"
            (closed)="clearFocus()"
          />
        }
      </div>

      <!-- live activity feed — full width, below the board in the page stack -->
      @if (graph() && layout().nodes.length) {
        <section
          class="feed"
          (mouseenter)="feedEnter()"
          (mouseleave)="feedLeave()"
        >
          <div class="feed__head">
            <span>Live activity</span>
            @if (feedHeld()) {
              <span class="feed__hold">
                held while hovering
                @if (feedPending()) {
                  · {{ feedPending() }} new
                }
              </span>
            }
            <span class="spacer"></span>
            <div class="feed__filters">
              <button
                class="fbtn"
                [class.fbtn--on]="feedFilter() === 'all'"
                (click)="feedFilter.set('all')"
              >
                All
              </button>
              <button
                class="fbtn"
                [class.fbtn--on]="feedFilter() === 'warn'"
                (click)="feedFilter.set('warn')"
              >
                Problems
              </button>
            </div>
          </div>
          @if (!feedView().length) {
            <p class="muted sm feed__empty">
              {{
                feedFilter() === "warn" && feed().length
                  ? "No problem rows — every listener is approving."
                  : "No traffic yet. Send a transaction and the nodes light up."
              }}
            </p>
          } @else {
            <div class="ftable" role="table">
              <div class="ftable__hr" role="row">
                <span>Node</span><span>Type</span><span>Endpoint</span>
                <span class="num">Δ msgs</span><span class="num">Rate</span>
                <span class="num">Total</span><span>Status</span
                ><span class="num">Time</span>
              </div>
              @for (f of feedView(); track f.key) {
                <div
                  class="ftable__row"
                  [attr.data-tone]="f.tone"
                  role="row"
                  [class.ftable__row--sel]="selected() === f.id"
                  (click)="selectNode(f.id)"
                  title="Focus this node on the map"
                >
                  <span class="fc-node">
                    <span class="fc-ico"
                      ><pp-icon [name]="kindIcon(f.kind)" [size]="14"
                    /></span>
                    <span class="fc-lbl">{{ f.label }}</span>
                  </span>
                  <span class="muted">{{ f.kind }}</span>
                  <span class="mono muted">{{ f.sub }}</span>
                  <span class="num fc-delta">+{{ f.delta }}</span>
                  <span class="num">{{ f.rate }}/s</span>
                  <span class="num muted">{{ f.total }}</span>
                  <span class="fc-status" [attr.data-tone]="f.tone">{{
                    f.status
                  }}</span>
                  <span class="num mono muted">{{ f.time }}</span>
                </div>
              }
            </div>
          }
        </section>
      }

      @if (graph() && layout().nodes.length) {
        <div class="legend">
          <span
            ><span class="lg lg--initiator"></span> initiator (load run)</span
          >
          <span><span class="lg lg--topo"></span> topology</span>
          <span><span class="lg lg--participant"></span> participant</span>
          <span><span class="lg lg--simulator"></span> simulator</span>
          <span><span class="lg lg--group"></span> fleet (group)</span>
          <span><span class="lg lg--host"></span> external host</span>
          <span><span class="lg lg--clients"></span> clients</span>
          <span><span class="lg lg--bad"></span> unresolved target</span>
          <span class="legend__flow"
            ><span class="lg lg--flow"></span> live data exchange · click a node
            to focus</span
          >
        </div>
      }
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        padding: var(--space-5) var(--space-6);
        color: var(--text-primary);
      }
      h1 {
        margin: 0;
        font-size: var(--text-xl);
        font-weight: var(--weight-semibold);
      }
      .tm2 {
        margin-top: var(--space-4);
      }
      .muted {
        color: var(--text-muted);
      }
      .sm {
        font-size: var(--text-sm);
      }
      .spacer {
        flex: 1;
      }

      .kpis {
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: var(--space-3);
        margin-bottom: var(--space-4);
      }
      .kpi {
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-3) var(--space-4);
        box-shadow: var(--shadow-xs);
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .kpi__k {
        font-size: var(--text-sm);
        color: var(--text-muted);
      }
      .kpi__v {
        font-size: var(--text-2xl);
        font-weight: var(--weight-semibold);
        line-height: 1;
        display: inline-flex;
        align-items: baseline;
        gap: 5px;
      }
      .kpi__v--ok {
        color: var(--color-success);
      }
      .kpi__v--bad {
        color: var(--color-danger);
      }
      .kpi__u,
      .kpi__of {
        font-size: var(--text-sm);
        font-weight: var(--weight-regular);
        color: var(--text-muted);
      }

      .bar {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        padding: var(--space-2-5) var(--space-4);
        margin-bottom: var(--space-4);
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        box-shadow: var(--shadow-xs);
      }
      .live {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
        color: var(--color-success);
      }
      .live--paused {
        color: var(--text-muted);
      }
      .live--stale {
        color: var(--color-warning);
      }
      .live .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: currentColor;
      }

      .stale {
        display: flex;
        align-items: flex-start;
        gap: var(--space-2);
        padding: var(--space-2-5) var(--space-4);
        margin-bottom: var(--space-4);
        font-size: var(--text-sm);
        color: var(--text-primary);
        border: 1px solid
          color-mix(in srgb, var(--color-warning) 40%, var(--border));
        border-left: 3px solid var(--color-warning);
        border-radius: var(--radius-lg);
        background: color-mix(
          in srgb,
          var(--color-warning) 8%,
          var(--bg-surface)
        );
      }
      .stale pp-icon {
        color: var(--color-warning);
        flex: none;
        margin-top: 2px;
      }

      .boardwrap--stale {
        opacity: 0.55;
        filter: grayscale(0.35);
        transition: opacity 0.3s ease;
      }
      .focus {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: var(--text-sm);
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 12%, transparent);
        border-radius: var(--radius-full);
        padding: 2px var(--space-2) 2px var(--space-3);
      }
      .focus__x {
        display: inline-grid;
        place-items: center;
        width: 18px;
        height: 18px;
        border: none;
        background: transparent;
        color: inherit;
        cursor: pointer;
        border-radius: 50%;
      }
      .focus__x:hover {
        background: color-mix(in srgb, var(--brand) 20%, transparent);
      }
      .zoom {
        display: inline-flex;
        align-items: center;
        gap: var(--space-1);
      }
      .zbtn {
        display: inline-grid;
        place-items: center;
        min-width: 28px;
        height: 28px;
        padding: 0 6px;
        border: 1px solid var(--border);
        background: var(--bg-surface);
        color: var(--text-secondary);
        border-radius: var(--radius-sm);
        cursor: pointer;
        font-size: var(--text-sm);
      }
      .zbtn:hover {
        background: var(--bg-subtle);
        color: var(--text-primary);
      }
      .zval {
        font-size: var(--text-xs);
        color: var(--text-muted);
        min-width: 36px;
        text-align: center;
        font-variant-numeric: tabular-nums;
      }

      .split {
        display: block;
      }
      /* selecting a node opens the action drawer as a right column */
      .split--drawer {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 320px;
        gap: var(--space-4);
        align-items: start;
      }
      @media (max-width: 1100px) {
        .split--drawer {
          grid-template-columns: 1fr;
        }
      }

      .canvas {
        overflow: auto;
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        background:
          radial-gradient(circle at 1px 1px, var(--border) 1px, transparent 0) 0
            0 / 26px 26px,
          var(--bg-base);
        box-shadow: var(--shadow-xs) inset;
        max-height: calc(100vh - 360px);
        min-height: 320px;
        touch-action: pan-x pan-y;
      }
      .canvas--panning {
        cursor: grabbing;
        user-select: none;
      }
      svg {
        display: block;
      }

      /* motion hygiene — respect the OS setting */
      @media (prefers-reduced-motion: reduce) {
        .edge__flow {
          animation: none;
        }
        .edge__packet {
          display: none;
        }
        .pulse {
          animation: none;
        }
      }

      .cont__box {
        fill: color-mix(in srgb, var(--brand) 4%, transparent);
        stroke: color-mix(in srgb, var(--brand) 32%, var(--border));
        stroke-width: 1.5;
        stroke-dasharray: 6 5;
      }
      .cont__lbl {
        fill: var(--brand);
        font-size: 12px;
        font-weight: 600;
        font-family: var(--font-sans);
      }
      .cont__lbl--link {
        cursor: pointer;
        pointer-events: all;
      }
      .cont__lbl--link:hover {
        text-decoration: underline;
      }
      .cont__edit {
        fill: var(--muted);
        font-weight: 400;
        font-size: 11px;
      }

      .edge__wire {
        fill: none;
        stroke: var(--border-strong);
        stroke-width: 1.6;
      }
      .arrowhead {
        fill: var(--border-strong);
      }
      .edge[data-kind="client"] .edge__wire {
        stroke-dasharray: 3 4;
        opacity: 0.4;
        stroke-width: 1.1;
      }
      .edge[data-kind="member"] .edge__wire {
        stroke-dasharray: 2 3;
      }
      .edge--active .edge__wire {
        stroke: color-mix(in srgb, var(--brand) 55%, var(--border-strong));
      }
      .edge[data-kind="client"].edge--active .edge__flow {
        opacity: 0.4;
      }
      .edge__flow {
        fill: none;
        stroke: var(--brand);
        stroke-width: 2;
        stroke-linecap: round;
        stroke-dasharray: 7 9;
        animation: tm2-dash 0.6s linear infinite;
        opacity: 0.85;
      }
      @keyframes tm2-dash {
        to {
          stroke-dashoffset: -32;
        }
      }
      .edge__packet {
        fill: var(--brand);
      }
      .edge__lbl {
        fill: var(--text-secondary);
        font-size: 10.5px;
        font-family: var(--font-mono);
        paint-order: stroke;
        stroke: var(--bg-base);
        stroke-width: 3px;
        stroke-linejoin: round;
      }

      .node {
        cursor: pointer;
      }
      .node {
        cursor: grab;
      }
      .node:active {
        cursor: grabbing;
      }
      .node text,
      .node__ico {
        user-select: none;
      }
      .node__box {
        fill: var(--bg-surface);
        stroke: var(--border);
      }
      .node--active .node__box {
        stroke: color-mix(in srgb, var(--brand) 60%, var(--border));
      }
      .node--sel .node__box {
        stroke: var(--brand);
        stroke-width: 2;
      }
      /* external endpoints (hosts / clients) read as outside the simulated network */
      .node[data-kind="host"] .node__box,
      .node[data-kind="clients"] .node__box {
        stroke-dasharray: 5 4;
        fill: var(--bg-base);
      }
      .node[data-status="unresolved"] .node__box {
        stroke: var(--color-danger);
        stroke-dasharray: 5 4;
      }
      .node__ext {
        fill: var(--text-muted);
        font-size: 9.5px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .node__ext--bad {
        fill: var(--color-danger);
      }
      .node__ext--fleet {
        fill: var(--brand);
      }
      .node__ring {
        fill: none;
        stroke: var(--color-warning);
        stroke-width: 2;
      }
      .node__accent {
        fill: var(--text-muted);
      }
      .node[data-kind="initiator"] .node__accent {
        fill: var(--brand);
      }
      .node[data-kind="initiator"] .node__box {
        stroke: color-mix(in srgb, var(--brand) 40%, var(--border));
      }
      .node[data-kind="driver"] .node__accent {
        fill: var(--brand);
      }
      .node[data-kind="participant"] .node__accent {
        fill: var(--color-info, #38bdf8);
      }
      .node[data-kind="simulator"] .node__accent {
        fill: var(--color-success);
      }
      .node[data-kind="host"] .node__accent {
        fill: var(--color-warning);
      }
      .node[data-kind="clients"] .node__accent {
        fill: var(--text-secondary);
      }
      .node[data-kind="group"] .node__accent {
        fill: var(--brand);
      }
      .node[data-kind="group"] .node__box {
        stroke-dasharray: 0;
      }
      .node[data-status="unresolved"] .node__accent {
        fill: var(--color-danger);
      }
      .node[data-status="unresolved"] .node__status {
        fill: var(--color-danger);
      }
      .node[data-status="down"] .node__box {
        fill: var(--bg-base);
      }
      .node[data-status="down"] .node__label {
        fill: var(--text-secondary);
      }
      .node__status {
        fill: var(--text-muted);
      }
      .node[data-status="up"] .node__status {
        fill: var(--color-success);
      }
      .node[data-status="degraded"] .node__status,
      .node[data-status="external"] .node__status {
        fill: var(--color-warning);
      }
      .node__ico {
        display: inline-flex;
        color: var(--text-secondary);
      }
      .node--active .node__ico,
      .node--sel .node__ico {
        color: var(--brand);
      }
      .node__label {
        fill: var(--text-primary);
        font-size: 13.5px;
        font-weight: 600;
      }
      .node__sub {
        fill: var(--text-muted);
        font-size: 11px;
        font-family: var(--font-mono);
      }
      .node__track {
        fill: var(--bg-subtle);
      }
      .node__bar {
        fill: var(--brand);
      }
      .node__ok {
        fill: var(--color-success);
      }
      .node__decl {
        fill: var(--color-danger);
      }
      .node__stat {
        fill: var(--text-muted);
        font-size: 11.5px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
      }
      .node__stat--hot {
        fill: var(--brand);
      }

      @keyframes tm2-pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.25;
        }
      }
      .pulse {
        animation: tm2-pulse 0.9s ease-in-out infinite;
      }

      .trend {
        margin-top: var(--space-3);
      }
      .trend__head {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        margin-bottom: 4px;
        font-size: var(--text-xs);
        color: var(--text-muted);
      }
      .trend__leg {
        display: inline-flex;
        align-items: center;
        gap: 5px;
      }
      .trend__leg i {
        width: 10px;
        height: 2px;
        display: inline-block;
      }
      .lg--tps {
        background: var(--color-info, #38bdf8);
      }
      .lg--decl {
        background: var(--color-danger);
      }
      .trend__svg {
        width: 100%;
        height: 56px;
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
      }
      .trend__area {
        fill: var(--color-info, #38bdf8);
        opacity: 0.13;
      }
      .trend__line {
        fill: none;
        stroke: var(--color-info, #38bdf8);
        stroke-width: 1.6;
        vector-effect: non-scaling-stroke;
      }
      .trend__decl {
        fill: none;
        stroke: var(--color-danger);
        stroke-width: 1.4;
        opacity: 0.85;
        vector-effect: non-scaling-stroke;
      }

      .feed {
        display: block;
        margin-top: var(--space-4);
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-3) var(--space-4);
        box-shadow: var(--shadow-xs);
      }
      .feed__head {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
        color: var(--text-muted);
        margin-bottom: var(--space-2);
      }
      .feed__hold {
        text-transform: none;
        letter-spacing: normal;
        color: var(--color-warning);
        font-weight: var(--weight-medium);
      }
      .feed__filters {
        display: inline-flex;
        gap: 2px;
        border: 1px solid var(--border);
        border-radius: var(--radius-full);
        padding: 2px;
        background: var(--bg-base);
      }
      .fbtn {
        border: none;
        cursor: pointer;
        background: transparent;
        color: var(--text-muted);
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
        padding: 2px var(--space-2-5);
        border-radius: var(--radius-full);
      }
      .fbtn:hover {
        color: var(--text-primary);
      }
      .fbtn--on {
        background: var(--bg-surface);
        color: var(--brand);
        box-shadow: var(--shadow-xs);
      }
      .feed__empty {
        margin: var(--space-2) 0;
      }
      .ftable {
        display: grid;
        grid-template-columns:
          minmax(140px, 1.4fr) 90px minmax(110px, 1fr)
          78px 64px 72px 90px 78px;
        font-size: var(--text-xs);
        max-height: 340px;
        overflow: auto;
      }
      .ftable__hr,
      .ftable__row {
        display: contents;
      }
      .ftable__hr > span {
        position: sticky;
        top: 0;
        z-index: 1;
        background: var(--bg-surface);
        padding: var(--space-1) var(--space-2);
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
        color: var(--text-muted);
        border-bottom: 1px solid var(--border);
      }
      .ftable__row > span {
        padding: var(--space-1-5) var(--space-2);
        border-bottom: 1px solid var(--border);
        align-self: center;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .ftable__row {
        cursor: pointer;
      }
      .ftable__row:hover > span {
        background: var(--bg-subtle);
      }
      .ftable__row--sel > span {
        background: color-mix(in srgb, var(--brand) 8%, transparent);
      }
      .num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .mono {
        font-family: var(--font-mono);
        font-size: 10.5px;
      }
      .fc-node {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-weight: var(--weight-medium);
      }
      .fc-ico {
        display: inline-flex;
        color: var(--text-secondary);
        flex: none;
      }
      .fc-lbl {
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .fc-delta {
        font-weight: var(--weight-semibold);
        color: var(--brand);
      }
      .fc-status {
        text-transform: capitalize;
        font-size: 10.5px;
      }
      .ftable__row[data-tone="ok"] .fc-status {
        color: var(--color-success);
      }
      .ftable__row[data-tone="warn"] .fc-status {
        color: var(--color-warning);
      }
      .ftable__row[data-tone="info"] .fc-status {
        color: var(--brand);
      }

      .state {
        padding: var(--space-8);
        text-align: center;
      }
      .state--empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: var(--space-2);
        border: 1px dashed var(--border-strong);
        border-radius: var(--radius-xl);
        background: var(--bg-surface);
      }
      .state__icon {
        display: grid;
        place-items: center;
        width: 56px;
        height: 56px;
        border-radius: var(--radius-full);
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 12%, transparent);
        margin-bottom: var(--space-1);
      }
      .state--empty h3 {
        margin: 0;
        font-size: var(--text-lg);
      }
      .state--empty p {
        max-width: 46ch;
        margin: 0;
      }

      .legend {
        display: flex;
        flex-wrap: wrap;
        gap: var(--space-4);
        margin-top: var(--space-3);
        font-size: var(--text-sm);
        color: var(--text-muted);
      }
      .legend span {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
      }
      .lg {
        width: 12px;
        height: 12px;
        border-radius: 3px;
        flex: none;
      }
      .lg--initiator {
        background: var(--brand);
      }
      .lg--topo {
        background: transparent;
        border: 1.5px dashed color-mix(in srgb, var(--brand) 45%, var(--border));
      }
      .lg--participant {
        background: var(--color-info, #38bdf8);
      }
      .lg--simulator {
        background: var(--color-success);
      }
      .lg--group {
        background: var(--brand);
      }
      .lg--host {
        background: var(--color-warning);
      }
      .lg--clients {
        background: var(--text-secondary);
      }
      .lg--bad {
        background: var(--color-danger);
      }
      .lg--flow {
        background: var(--brand);
        border-radius: var(--radius-full);
      }
      .legend__flow {
        margin-left: auto;
      }
    `,
  ],
})
export class TopologyMap2Component implements OnInit, OnDestroy {
  private readonly api = inject(RunApiService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  /** Shared graph feed (SSE with poll fallback) — one loop for all maps. */
  readonly stream = inject(NetworkGraphStreamService);
  /** The always-recording tape — feeds the trend so it survives navigation. */
  private readonly recorder = inject(ChronoscopeRecorderService);

  /** Deep-link a run container to its authoring canvas. */
  openCanvas(networkFlowId: string | undefined): void {
    if (networkFlowId) {
      this.router.navigate(["/network-flows"], {
        queryParams: { id: networkFlowId },
      });
    }
  }

  readonly TL_W = TL_W;
  readonly TL_H = TL_H;

  readonly graph = signal<NetworkGraph | null>(null);
  readonly paused = signal(false);
  /** Honest connection state — flips the Live badge to Stale (shared feed's). */
  readonly pollHealth = this.stream.pollHealth;
  readonly stale = this.stream.stale;
  /** Applies stream frames unless paused; re-applies the latest on resume. */
  private lastApplied: NetworkGraph | null = null;
  private readonly applyFx = effect(() => {
    const g = this.stream.graph();
    if (!g || this.paused() || g === this.lastApplied) return;
    this.lastApplied = g;
    this.deriveActivity(g);
    this.graph.set(g);
  });
  readonly zoom = signal(1);
  readonly selected = signal<string | null>(null);
  /** Manual node positions (drag overrides), persisted to localStorage. */
  readonly manualPos = signal<Record<string, { x: number; y: number }>>(
    this.loadPositions(),
  );
  private drag: {
    id: string;
    sx: number;
    sy: number;
    ox: number;
    oy: number;
    moved: boolean;
  } | null = null;

  private prevReceived = new Map<string, number>();
  private prevAt = 0;
  private feedKey = 0;
  private readonly rates = signal<Map<string, number>>(new Map());
  private readonly actives = signal<Set<string>>(new Set());
  readonly feed = signal<FeedEntry[]>([]);
  /** Trend samples straight off the recorder tape — the sparkline arrives
   *  pre-filled and survives navigation. Empty while a replay file is loaded
   *  in the Chronoscope (that tape is history, not this board). */
  readonly timeline = computed<TLSample[]>(() => {
    if (this.recorder.source() !== "live") return [];
    return this.recorder.frames().map((f) => ({
      tps: f.tps,
      decl: f.g.nodes.reduce((a, n) => a + declCount(n), 0),
    }));
  });
  /** Real span of the tape ("47s" / "6m") — frame count × 2s would lie
   *  across gaps recorded while no map was open. */
  readonly trendSpan = computed(() => {
    const fr = this.recorder.frames();
    if (fr.length < 2 || this.recorder.source() !== "live") return "";
    const secs = Math.round((fr[fr.length - 1].at - fr[0].at) / 1000);
    return secs < 60 ? secs + "s" : Math.round(secs / 60) + "m";
  });

  // -- feed view: hover-freeze + tone filter -------------------------------
  /** Snapshot shown while the pointer is over the feed (rows keep buffering). */
  private readonly feedFrozen = signal<FeedEntry[] | null>(null);
  readonly feedFilter = signal<"all" | "warn">("all");
  readonly feedView = computed(() => {
    const rows = this.feedFrozen() ?? this.feed();
    return this.feedFilter() === "warn"
      ? rows.filter((r) => r.tone === "warn")
      : rows;
  });
  readonly feedHeld = computed(() => this.feedFrozen() !== null);
  /** Rows that arrived while the view was held under the pointer. */
  readonly feedPending = computed(() => {
    const frozen = this.feedFrozen();
    if (!frozen) return 0;
    const maxKey = frozen.length ? frozen[0].key : -1;
    return this.feed().filter((r) => r.key > maxKey).length;
  });
  feedEnter() {
    this.feedFrozen.set(this.feed());
  }
  feedLeave() {
    this.feedFrozen.set(null);
  }

  readonly zoomPct = computed(() => Math.round(this.zoom() * 100));
  readonly totalRate = computed(() => {
    let t = 0;
    for (const v of this.rates().values()) t += v;
    return Math.round(t);
  });

  /** Cumulative approval rate across all listeners (DE39 "00" / all replies). */
  readonly approval = computed(() => {
    const g = this.graph();
    if (!g) return null;
    let ok = 0;
    let total = 0;
    for (const n of g.nodes) {
      if (!n.by_rc) continue;
      for (const [rc, v] of Object.entries(n.by_rc)) {
        total += v;
        if (rc === "00") ok += v;
      }
    }
    return total ? Math.round((ok / total) * 100) : null;
  });
  readonly declined = computed(() => {
    const g = this.graph();
    if (!g) return 0;
    let d = 0;
    for (const n of g.nodes) {
      if (!n.by_rc) continue;
      for (const [rc, v] of Object.entries(n.by_rc)) if (rc !== "00") d += v;
    }
    return d;
  });

  readonly listenerNodes = computed(
    () =>
      this.graph()?.nodes.filter(
        (n) => n.kind === "participant" || n.kind === "simulator",
      ) ?? [],
  );
  readonly listenerCount = computed(() => this.listenerNodes().length);
  readonly nodesUp = computed(
    () => this.listenerNodes().filter((n) => n.status === "up").length,
  );
  readonly activePeers = computed(
    () => this.graph()?.nodes.reduce((a, n) => a + (n.peers || 0), 0) ?? 0,
  );

  readonly selectedLabel = computed(() => {
    const id = this.selected();
    return this.graph()?.nodes.find((n) => n.id === id)?.label ?? "";
  });
  /** The selected node with its layout-derived live rate — feeds the drawer. */
  readonly selectedNode = computed(() => {
    const id = this.selected();
    return id ? (this.layout().nodes.find((n) => n.id === id) ?? null) : null;
  });
  /** Adjacency (undirected) for click-to-focus dimming. */
  private readonly adjacency = computed(() => {
    const m = new Map<string, Set<string>>();
    for (const e of this.graph()?.edges ?? []) {
      (m.get(e.source) ?? m.set(e.source, new Set()).get(e.source)!).add(
        e.target,
      );
      (m.get(e.target) ?? m.set(e.target, new Set()).get(e.target)!).add(
        e.source,
      );
    }
    return m;
  });

  readonly layout = computed(() => this.computeLayout(this.graph()));

  // -- throughput timeline paths --
  readonly tlLine = computed(() => this.tlPoints((s) => s.tps, true));
  readonly tlArea = computed(() => {
    const pts = this.tlPoints((s) => s.tps, true);
    if (!pts) return "";
    return `0,${TL_H} ${pts} ${TL_W},${TL_H}`;
  });
  readonly tlDecl = computed(() => {
    const tl = this.timeline();
    const deltas = tl.map((s, i) =>
      i ? Math.max(0, s.decl - tl[i - 1].decl) : 0,
    );
    return this.seriesPoints(deltas);
  });

  // -- shareable URL state: ?focus=<node>&zoom=<pct> -----------------------
  /** Set after the initial params are read, so the sync effect can't clobber
   *  an incoming deep link with defaults. */
  private urlReady = false;
  private readonly urlFx = effect(() => {
    const focus = this.selected();
    const zoom = this.zoomPct();
    if (!this.urlReady) return;
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { focus: focus || null, zoom: zoom === 100 ? null : zoom },
      queryParamsHandling: "merge",
      replaceUrl: true, // no history spam while dragging the zoom
    });
  });

  ngOnInit() {
    const qp = this.route.snapshot.queryParamMap;
    const focus = qp.get("focus");
    if (focus) this.selected.set(focus);
    const zoom = Number(qp.get("zoom"));
    if (zoom) this.zoom.set(this.clampZoom(zoom / 100));
    this.urlReady = true;
    this.stream.acquire();
  }
  ngOnDestroy() {
    this.stream.release();
  }

  togglePause() {
    // resuming re-applies the newest stream frame via the effect
    this.paused.update((p) => !p);
  }

  // -- zoom & pan ----------------------------------------------------------
  @ViewChild("canvasBox") canvasBox?: ElementRef<HTMLDivElement>;
  /** Background drag scrolls the canvas (nodes stopPropagation their own). */
  private pan: {
    sx: number;
    sy: number;
    sl: number;
    st: number;
    moved: boolean;
  } | null = null;
  readonly panning = signal(false);
  /** Tab hidden — skip packet/pulse animations nobody can see. */
  readonly docHidden = signal(
    typeof document !== "undefined" && document.hidden,
  );

  private clampZoom(z: number): number {
    return Math.min(1.8, Math.max(0.3, Math.round(z * 100) / 100));
  }
  zoomBy(d: number) {
    this.zoom.update((z) => this.clampZoom(z + d));
  }
  resetZoom() {
    this.zoom.set(1);
  }
  /** Scale so the whole board fits the visible canvas. */
  fitToView() {
    const box = this.canvasBox?.nativeElement;
    const l = this.layout();
    if (!box || !l.width || !l.height) {
      this.zoom.set(1);
      return;
    }
    this.zoom.set(
      this.clampZoom(
        Math.min(
          (box.clientWidth - 8) / l.width,
          (box.clientHeight - 8) / l.height,
        ),
      ),
    );
    box.scrollLeft = 0;
    box.scrollTop = 0;
  }
  /** Ctrl/⌘+wheel (trackpad pinch) zooms, anchored at the cursor. */
  onWheel(ev: WheelEvent) {
    if (!ev.ctrlKey && !ev.metaKey) return; // plain scroll keeps scrolling
    ev.preventDefault();
    const box = this.canvasBox?.nativeElement;
    const z1 = this.zoom();
    const z2 = this.clampZoom(z1 * (ev.deltaY > 0 ? 0.9 : 1.1));
    if (z2 === z1) return;
    this.zoom.set(z2);
    if (box) {
      const r = box.getBoundingClientRect();
      const cx = ev.clientX - r.left;
      const cy = ev.clientY - r.top;
      const k = z2 / z1;
      const sl = (box.scrollLeft + cx) * k - cx;
      const st = (box.scrollTop + cy) * k - cy;
      // the svg resizes on the next render pass — anchor after it
      requestAnimationFrame(() => {
        box.scrollLeft = sl;
        box.scrollTop = st;
      });
    }
  }
  onCanvasDown(ev: PointerEvent) {
    if (ev.button !== 0) return;
    const box = this.canvasBox?.nativeElement;
    if (!box) return;
    this.pan = {
      sx: ev.clientX,
      sy: ev.clientY,
      sl: box.scrollLeft,
      st: box.scrollTop,
      moved: false,
    };
  }
  @HostListener("document:visibilitychange")
  onVisibility(): void {
    this.docHidden.set(document.hidden);
  }
  selectNode(id: string) {
    this.selected.update((c) => (c === id ? null : id));
  }

  // -- drag-to-reposition node tiles -------------------------------------
  private loadPositions(): Record<string, { x: number; y: number }> {
    try {
      return JSON.parse(localStorage.getItem("tm2-positions") || "{}");
    } catch {
      return {};
    }
  }
  private savePositions(): void {
    try {
      localStorage.setItem("tm2-positions", JSON.stringify(this.manualPos()));
    } catch {
      /* ignore quota / disabled storage */
    }
  }
  readonly hasManualLayout = computed(
    () => Object.keys(this.manualPos()).length > 0,
  );
  resetLayout(): void {
    this.manualPos.set({});
    try {
      localStorage.removeItem("tm2-positions");
    } catch {
      /* ignore */
    }
  }
  onNodeDown(ev: PointerEvent, n: PNode): void {
    ev.stopPropagation();
    this.drag = {
      id: n.id,
      sx: ev.clientX,
      sy: ev.clientY,
      ox: n.x,
      oy: n.y,
      moved: false,
    };
  }
  @HostListener("document:pointermove", ["$event"])
  onDocMove(ev: PointerEvent): void {
    const pan = this.pan;
    if (pan) {
      const dx = ev.clientX - pan.sx;
      const dy = ev.clientY - pan.sy;
      if (!pan.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
      pan.moved = true;
      this.panning.set(true);
      const box = this.canvasBox?.nativeElement;
      if (box) {
        box.scrollLeft = pan.sl - dx;
        box.scrollTop = pan.st - dy;
      }
      return;
    }
    const d = this.drag;
    if (!d) return;
    const z = this.zoom() || 1;
    const dx = (ev.clientX - d.sx) / z;
    const dy = (ev.clientY - d.sy) / z;
    if (!d.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
    d.moved = true;
    this.manualPos.update((m) => ({
      ...m,
      [d.id]: { x: Math.round(d.ox + dx), y: Math.round(d.oy + dy) },
    }));
  }
  @HostListener("document:keydown.escape")
  onEscape(): void {
    this.clearFocus();
  }
  @HostListener("document:pointerup")
  onDocUp(): void {
    const pan = this.pan;
    if (pan) {
      this.pan = null;
      this.panning.set(false);
      if (!pan.moved) this.clearFocus(); // background click clears focus
      return;
    }
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    if (d.moved) this.savePositions();
    else this.selectNode(d.id); // a click (no drag) still selects
  }
  clearFocus() {
    this.selected.set(null);
  }
  nodeOpacity(n: PNode): number {
    const s = this.selected();
    if (!s) return 1;
    if (n.id === s) return 1;
    return this.adjacency().get(s)?.has(n.id) ? 1 : 0.28;
  }
  edgeOpacity(e: PEdge): number {
    const s = this.selected();
    if (!s) return 1;
    return e.source === s || e.target === s ? 1 : 0.18;
  }

  /** Manual refresh — applies a fresh frame even while paused. */
  refresh() {
    this.api.networkGraph().subscribe({
      next: (g: NetworkGraph) => {
        this.lastApplied = g;
        this.deriveActivity(g);
        this.graph.set(g);
        this.pollHealth.markOk();
      },
      error: () => this.pollHealth.markError(),
    });
  }

  private deriveActivity(g: NetworkGraph) {
    const now = Date.now();
    const dt = this.prevAt ? Math.max(0.5, (now - this.prevAt) / 1000) : 0;
    const rates = new Map<string, number>();
    const actives = new Set<string>();
    const newRows: FeedEntry[] = [];
    const stamp = new Date().toLocaleTimeString();

    for (const n of g.nodes) {
      const prev = this.prevReceived.get(n.id);
      if (prev != null && dt > 0 && n.received > prev) {
        const delta = n.received - prev;
        const rate = Math.round(delta / dt);
        rates.set(n.id, rate);
        actives.add(n.id);
        if (n.kind === "participant" || n.kind === "simulator") {
          newRows.push({
            key: this.feedKey++,
            id: n.id,
            time: stamp,
            label: n.label,
            kind: n.kind,
            sub: this.nodeSub(n),
            status: n.status,
            delta,
            rate,
            total: n.received,
            tone: this.toneFor(n),
          });
        }
      }
      this.prevReceived.set(n.id, n.received);
    }

    const live = new Set(g.nodes.map((n) => n.id));
    for (const id of [...this.prevReceived.keys()])
      if (!live.has(id)) this.prevReceived.delete(id);

    this.prevAt = now;
    this.rates.set(rates);
    this.actives.set(actives);
    // (the throughput trend comes from the recorder tape, not derived here)

    if (newRows.length) {
      newRows.sort((a, b) => b.delta - a.delta);
      this.feed.update((cur) => [...newRows, ...cur].slice(0, 30));
    }
  }

  private toneFor(n: GraphNode): "ok" | "warn" | "info" {
    const share = this.approvalShare(n);
    if (share === null) return "info";
    return share >= 0.9 ? "ok" : "warn";
  }
  /** Approve share from a node's cumulative response mix, or null if no replies. */
  private approvalShare(n: GraphNode): number | null {
    const rc = n.by_rc;
    if (!rc) return null;
    let ok = 0;
    let total = 0;
    for (const [k, v] of Object.entries(rc)) {
      total += v;
      if (k === "00") ok += v;
    }
    return total ? ok / total : null;
  }

  // -- timeline helpers --
  private tlPoints(
    pick: (s: TLSample) => number,
    _scaleByMax: boolean,
  ): string {
    return this.seriesPoints(this.timeline().map(pick));
  }
  private seriesPoints(vals: number[]): string {
    if (!vals.length) return "";
    const max = Math.max(1, ...vals);
    const n = Math.max(1, vals.length - 1);
    return vals
      .map(
        (v, i) =>
          `${((i / n) * TL_W).toFixed(1)},${(TL_H - (v / max) * (TL_H - 6)).toFixed(1)}`,
      )
      .join(" ");
  }

  private computeLayout(g: NetworkGraph | null): {
    nodes: PNode[];
    edges: PEdge[];
    containers: PContainer[];
    width: number;
    height: number;
  } {
    if (!g || !g.nodes.length)
      return { nodes: [], edges: [], containers: [], width: 100, height: 100 };

    // Container-aware layered layout with crossing reduction and neighbour
    // alignment (see tm2-layout.ts). Manual drag overrides win over the
    // computed positions (edges/containers recompute from these, so they
    // follow a dragged node). Reading the signal here makes the layout
    // recompute on every drag.
    const auto = computeMapLayout(g, this.manualPos());
    const pos = auto.pos;
    const lane = auto.lane;

    const rates = this.rates();
    const actives = this.actives();
    const nodes: PNode[] = g.nodes.map((n) => {
      const p = pos.get(n.id)!;
      const rate = rates.get(n.id) || 0;
      let rcTotal = 0;
      let ok = 0;
      if (n.by_rc) {
        for (const [k, v] of Object.entries(n.by_rc)) {
          rcTotal += v;
          if (k === "00") ok += v;
        }
      }
      const okW = rcTotal ? (ok / rcTotal) * BAR_W : 0;
      const declW = rcTotal ? BAR_W - okW : 0;
      const declShare = rcTotal ? 1 - ok / rcTotal : 0;
      let health: PNode["health"] = "idle";
      if (n.status === "down") health = "down";
      else if (rcTotal && declShare > 0.25) health = "warn";
      else if (n.status === "up") health = "ok";
      return {
        ...n,
        x: p.x,
        y: p.y,
        lane: lane.get(n.id)!,
        rate,
        active: actives.has(n.id),
        rcTotal,
        okW,
        declW,
        barW: rate > 0 ? Math.min(BAR_W, 12 + rate * 3) : 0,
        health,
      };
    });

    const edges: PEdge[] = [];
    for (const e of g.edges) {
      const s = pos.get(e.source);
      const t = pos.get(e.target);
      if (!s || !t) continue;
      const x1 = s.x + NODE_W;
      const y1 = s.y + NODE_H / 2;
      const x2 = t.x;
      const y2 = t.y + NODE_H / 2;
      const dx = Math.max(40, Math.abs(x2 - x1) * 0.5);
      const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
      // Light an edge when either end saw traffic — synthetic pass-through nodes
      // (driver, fleet, clients) never accumulate messages, so a target-only rule
      // would leave the segment into them dark even while data flows through.
      const active = actives.has(e.target) || actives.has(e.source);
      // Place the label ~38% from the source so long fan-out edges keep their
      // label in open space near the origin instead of over the target node.
      edges.push({
        ...e,
        d,
        active,
        midX: x1 + (x2 - x1) * 0.38,
        midY: y1 + (y2 - y1) * 0.38,
      });
    }

    // topology containers + canvas size come from the layout module (frames
    // are tight around member positions and the canvas grows to fit drags).
    return {
      nodes,
      edges,
      containers: auto.containers,
      width: auto.width,
      height: auto.height,
    };
  }

  pathId(e: GraphEdge): string {
    return "tm2p-" + e.id.replace(/[^a-zA-Z0-9]/g, "-");
  }
  packetDur(e: PEdge): string {
    const r = this.rates().get(e.target) || 0;
    const secs = r > 50 ? 0.6 : r > 10 ? 0.9 : 1.3;
    return secs + "s";
  }
  /** Function icon per node kind (matches the sidebar tile icons). */
  kindIcon(kind: string): string {
    switch (kind) {
      case "participant":
        return "flow";
      case "simulator":
        return "server";
      case "group":
        return "group";
      case "driver":
        return "play";
      case "clients":
        return "user";
      case "host":
        return "cpu";
      default:
        return "plug";
    }
  }

  nodeSub(n: GraphNode): string {
    if (n.kind === "driver") return "topology driver";
    if (n.kind === "clients") return n.sublabel || "inbound";
    if (n.kind === "host") return n.sublabel || "external";
    const proto = n.protocol || n.sublabel || "";
    if (n.port) return `${proto} :${n.port}`;
    return n.status === "down" ? "stopped" : proto;
  }

  /** Label clipped to the space left of the right-side stat/flag. */
  nodeLabel(n: PNode): string {
    const hasRight =
      n.status === "unresolved" ||
      (n.kind !== "driver" && n.kind !== "clients" && !!this.nodeStat(n));
    const max = hasRight ? 16 : 24;
    return n.label.length > max ? n.label.slice(0, max - 1) + "…" : n.label;
  }

  /** Top-right node stat: live rate when active, else cumulative volume. */
  nodeStat(n: PNode): string {
    if (n.active && n.rate > 0) return n.rate + "/s";
    if (n.received > 0)
      return n.received >= 1000
        ? (n.received / 1000).toFixed(1) + "k"
        : "" + n.received;
    return "";
  }
}
