import {
  Component,
  OnInit,
  OnDestroy,
  inject,
  signal,
  computed,
  effect,
  HostListener,
  ChangeDetectionStrategy,
} from "@angular/core";

import { ButtonComponent } from "../../shared/ui/button.component";
import { IconComponent } from "../../shared/ui/icon.component";
import { ActivatedRoute, Router } from "@angular/router";

import { NetworkGraphStreamService } from "../../run-monitor/network-graph-stream.service";
import { MapSwitcherComponent } from "../map-switcher.component";
import { CaptureChipComponent } from "../capture-chip.component";
import { ChronoscopeRecorderService } from "./chronoscope-recorder.service";
import { ChroniclePanelComponent } from "./chronicle-panel.component";
import { NodeInspectorComponent } from "./node-inspector.component";
import { TimeScrubberComponent } from "./time-scrubber.component";
import { computeOrbitalLayout } from "./orbital-layout";
import {
  Evt,
  Frame,
  POLL_MS,
  REdge,
  RNode,
  STRIP_W,
  StripMark,
  kindIcon,
  nodeSub,
  seriesPoints,
} from "./chronoscope.model";

/**
 * Topology Map 3 — "Chronoscope". Where map 2 is a live ops board, map 3
 * treats the network as a recording: an orbital radar scope, every
 * /network-graph poll kept as a frame in a rolling buffer, a scrubber +
 * transport that replays any past moment, a chronicle of auto-detected
 * events, a per-node inspector — and replay files that can be exported and
 * re-imported for offline incident review.
 *
 * The pieces live in ./: the recorder service owns the tape (buffer, events,
 * import/export), orbital-layout is the pure scope geometry, and the
 * inspector / chronicle / scrubber are presentational children. This
 * component wires them to the poll loop and owns the view state (playhead,
 * focus, playback).
 */
@Component({
  selector: "app-topology-map3",
  standalone: true,
  imports: [
    ButtonComponent,
    IconComponent,
    NodeInspectorComponent,
    ChroniclePanelComponent,
    TimeScrubberComponent,
    MapSwitcherComponent,
    CaptureChipComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="tm3">
      <!-- KPIs -->
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
        <div
          class="kpi kpi--view"
          [class.kpi--past]="!isLive() || source() === 'replay'"
        >
          <span class="kpi__k">Viewing</span>
          @if (source() === "replay") {
            <span class="kpi__v">{{
              isLive() ? "FILE END" : "T−" + behindSecs() + "s"
            }}</span>
          } @else if (isLive()) {
            @if (stale()) {
              <span class="kpi__v kpi__v--stale">STALE</span>
            } @else {
              <span class="kpi__v kpi__v--live">LIVE</span>
            }
          } @else {
            <span class="kpi__v">T−{{ behindSecs() }}s</span>
            <button class="golive" (click)="goLive()">Go live</button>
          }
        </div>
      </div>

      <!-- control bar -->
      <div class="bar">
        <app-map-switcher />
        <span
          class="live"
          [class.live--paused]="!isLive() || source() === 'replay'"
          [class.live--stale]="stale() && source() === 'live'"
        >
          <span
            class="dot"
            [class.pulse]="isLive() && source() === 'live' && !stale()"
          ></span>
          {{
            source() === "replay"
              ? "Replay file"
              : stale()
                ? "Stale"
                : isLive()
                  ? "Live"
                  : "Replay"
          }}
        </span>
        @if (source() === "replay") {
          <span class="focus">
            <pp-icon name="clock" [size]="13" /> {{ replayName() }}
            <button
              class="focus__x"
              (click)="backToLive()"
              aria-label="Back to live"
            >
              <pp-icon name="x" [size]="12" />
            </button>
          </span>
        } @else {
          <span class="muted sm">{{ bufferLabel() }}</span>
          <app-capture-chip />
        }
        @if (selected()) {
          <span class="focus">
            <pp-icon name="flow" [size]="13" /> {{ selectedLabel() }}
            <button
              class="focus__x"
              (click)="clearFocus()"
              aria-label="Clear focus"
            >
              <pp-icon name="x" [size]="12" />
            </button>
          </span>
        }
        @if (importError()) {
          <span class="err sm">{{ importError() }}</span>
        }
        <span class="spacer"></span>
        <input
          #importInput
          type="file"
          accept="application/json,.json"
          hidden
          (change)="onImportFile($event)"
        />
        <pp-button variant="ghost" size="sm" (click)="importInput.click()"
          >Import replay</pp-button
        >
        @if (frames().length) {
          <pp-button variant="ghost" size="sm" (click)="exportReplay()"
            >Export replay</pp-button
          >
        }
        @if (source() === "live") {
          <pp-button
            variant="secondary"
            size="sm"
            icon="activity"
            (click)="refresh()"
            >Refresh</pp-button
          >
        }
      </div>

      @if (stale() && source() === "live") {
        <div class="stale" role="alert">
          <pp-icon name="alert-circle" [size]="15" />
          <span>
            <strong>Lost contact with the orchestrator</strong> — retrying every
            {{ pollSecs }}s. Recording is interrupted;
            @if (pollHealth.sinceOk() !== null) {
              the last frame is {{ pollHealth.sinceOk() }}s old.
            } @else {
              no frames have been received yet.
            }
          </span>
        </div>
      }

      <div class="main">
        <!-- the scope -->
        <div class="scopewrap" [class.scopewrap--stale]="frozen()">
          @if (!frame()) {
            <div class="state">
              <span class="muted">Tuning the scope…</span>
            </div>
          } @else if (!layout().nodes.length) {
            <div class="state state--empty">
              <span class="state__icon"
                ><pp-icon name="network" [size]="28"
              /></span>
              <h3>Nothing on the scope</h3>
              <p class="muted">
                Start a topology, a participant flow or a simulator — it appears
                on the rings, and every moment from then on can be replayed.
              </p>
            </div>
          } @else {
            <div class="scope">
              <svg
                [attr.viewBox]="'0 0 ' + layout().w + ' ' + layout().h"
                preserveAspectRatio="xMidYMid meet"
                xmlns="http://www.w3.org/2000/svg"
              >
                <!-- orbit guides -->
                @for (r of layout().rings; track r) {
                  <circle
                    [attr.cx]="layout().cx"
                    [attr.cy]="layout().cy"
                    [attr.r]="r"
                    class="orbit"
                  />
                }

                <!-- radar sweep, only while watching live -->
                @if (
                  isLive() &&
                  !stale() &&
                  !docHidden() &&
                  source() === "live" &&
                  layout().sweepR > 0
                ) {
                  <g
                    [attr.transform]="
                      'translate(' + layout().cx + ',' + layout().cy + ')'
                    "
                  >
                    <g class="sweep">
                      <line
                        x1="0"
                        y1="0"
                        [attr.x2]="layout().sweepR"
                        y2="0"
                        class="sweep__line"
                      />
                      <animateTransform
                        attributeName="transform"
                        type="rotate"
                        from="0"
                        to="360"
                        dur="9s"
                        repeatCount="indefinite"
                      />
                    </g>
                  </g>
                }

                <!-- edges -->
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
                    />
                    @if (e.active && !frozen()) {
                      <path
                        [attr.d]="e.d"
                        class="edge__flow"
                        [style.stroke-width.px]="flowWidth(e)"
                      />
                      <circle r="3.4" class="edge__packet">
                        <animateMotion
                          [attr.dur]="packetDur(e)"
                          repeatCount="indefinite"
                          rotate="auto"
                        >
                          <mpath [attr.href]="'#' + pathId(e)" />
                        </animateMotion>
                      </circle>
                    }
                  </g>
                }

                <!-- nodes -->
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
                    (click)="selectNode(n.id)"
                  >
                    @if (n.active && !frozen()) {
                      <circle r="26" class="node__pulse" />
                    }
                    @if (n.health === "warn" || n.health === "down") {
                      <circle r="32" class="node__aura" />
                    }
                    <circle [attr.r]="26" class="node__body" />
                    <circle cx="18" cy="-18" r="4.5" class="node__statusdot" />
                    <foreignObject x="-11" y="-11" width="22" height="22">
                      <span
                        xmlns="http://www.w3.org/1999/xhtml"
                        class="node__ico"
                        ><pp-icon [name]="kindIcon(n.kind)" [size]="18"
                      /></span>
                    </foreignObject>
                    @if (nodeStat(n)) {
                      <text
                        y="-36"
                        text-anchor="middle"
                        class="node__stat"
                        [class.node__stat--hot]="n.active"
                      >
                        {{ nodeStat(n) }}
                        @if (n.trend === "up") {
                          <tspan class="trend trend--up">▲</tspan>
                        } @else if (n.trend === "down") {
                          <tspan class="trend trend--down">▼</tspan>
                        }
                      </text>
                    }
                    <text y="44" text-anchor="middle" class="node__label">
                      {{ nodeLabel(n) }}
                    </text>
                    <text y="58" text-anchor="middle" class="node__sub">
                      {{ nodeSub(n) }}
                    </text>
                    @if (n.status === "unresolved") {
                      <text y="72" text-anchor="middle" class="node__flag">
                        unresolved
                      </text>
                    } @else if (n.kind === "group") {
                      <text
                        y="72"
                        text-anchor="middle"
                        class="node__flag node__flag--fleet"
                      >
                        fleet
                      </text>
                    }
                  </g>
                }
              </svg>
            </div>
          }
        </div>

        <!-- side column -->
        <aside class="side">
          <app-chrono-inspector
            [node]="selectedNode()"
            [rate]="selectedRate()"
            [sparkVals]="sparkVals()"
          />
          <app-chrono-chronicle [events]="events()" (jump)="jumpTo($event)" />
        </aside>
      </div>

      <!-- time scrubber -->
      @if (frames().length > 1) {
        <app-chrono-scrubber
          [line]="stripLine()"
          [area]="stripArea()"
          [marks]="eventMarks()"
          [playheadX]="playheadX()"
          [spanLabel]="bufferSpanLabel()"
          [live]="isLive()"
          [endLabel]="source() === 'replay' ? 'end' : 'live'"
          [playing]="playing()"
          [speed]="speed()"
          (scrub)="onScrub($event)"
          (step)="stepFrame($event)"
          (playToggle)="togglePlay()"
          (speedCycle)="cycleSpeed()"
        />
      }

      @if (frame() && layout().nodes.length) {
        <div class="legend">
          <span><span class="lg lg--initiator"></span> initiator</span>
          <span><span class="lg lg--participant"></span> participant</span>
          <span><span class="lg lg--simulator"></span> simulator</span>
          <span><span class="lg lg--host"></span> external host</span>
          <span><span class="lg lg--clients"></span> clients</span>
          <span><span class="lg lg--bad"></span> unresolved</span>
          <span class="legend__flow"
            ><span class="lg lg--flow"></span> live exchange · outer→core =
            drive direction</span
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
      .tm3 {
        margin-top: var(--space-4);
      }
      .muted {
        color: var(--text-muted);
      }
      .sm {
        font-size: var(--text-sm);
      }
      .err {
        color: var(--color-danger);
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
      .kpi__v--live {
        color: var(--color-success);
        letter-spacing: 0.06em;
      }
      .kpi__v--stale {
        color: var(--color-warning);
        letter-spacing: 0.06em;
      }
      .kpi__u,
      .kpi__of {
        font-size: var(--text-sm);
        font-weight: var(--weight-regular);
        color: var(--text-muted);
      }
      .kpi--past {
        border-color: color-mix(in srgb, var(--brand) 45%, var(--border));
      }
      .golive {
        align-self: flex-start;
        margin-top: 2px;
        border: none;
        cursor: pointer;
        font-size: var(--text-xs);
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 12%, transparent);
        border-radius: var(--radius-full);
        padding: 2px var(--space-2-5);
      }
      .golive:hover {
        background: color-mix(in srgb, var(--brand) 20%, transparent);
      }

      .bar {
        display: flex;
        align-items: center;
        gap: var(--space-3);
        flex-wrap: wrap;
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

      .main {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 320px;
        gap: var(--space-4);
        align-items: start;
      }
      @media (max-width: 1100px) {
        .main {
          grid-template-columns: 1fr;
        }
      }
      .side {
        display: flex;
        flex-direction: column;
        gap: var(--space-4);
        min-width: 0;
      }

      .scopewrap--stale {
        opacity: 0.55;
        filter: grayscale(0.35);
        transition: opacity 0.3s ease;
      }

      .scope {
        overflow: auto;
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        background:
          radial-gradient(
            ellipse at center,
            color-mix(in srgb, var(--brand) 5%, transparent) 0%,
            transparent 62%
          ),
          var(--bg-base);
        box-shadow: var(--shadow-xs) inset;
        max-height: calc(100vh - 340px);
        min-height: 380px;
      }
      .scope svg {
        display: block;
        width: 100%;
        height: auto;
        min-height: 380px;
      }

      .orbit {
        fill: none;
        stroke: var(--border);
        stroke-dasharray: 2 6;
      }
      .sweep__line {
        stroke: color-mix(in srgb, var(--brand) 45%, transparent);
        stroke-width: 1.5;
      }

      .edge__wire {
        fill: none;
        stroke: var(--border-strong);
        stroke-width: 1.4;
      }
      .edge[data-kind="client"] .edge__wire {
        stroke-dasharray: 3 4;
        opacity: 0.4;
        stroke-width: 1;
      }
      .edge[data-kind="member"] .edge__wire {
        stroke-dasharray: 2 3;
      }
      .edge--active .edge__wire {
        stroke: color-mix(in srgb, var(--brand) 55%, var(--border-strong));
      }
      .edge__flow {
        fill: none;
        stroke: var(--brand);
        stroke-linecap: round;
        stroke-dasharray: 6 9;
        animation: tm3-dash 0.6s linear infinite;
        opacity: 0.8;
      }
      @keyframes tm3-dash {
        to {
          stroke-dashoffset: -30;
        }
      }
      .edge__packet {
        fill: var(--brand);
      }

      .node {
        cursor: pointer;
      }
      .node text,
      .node__ico {
        user-select: none;
      }
      .node__body {
        fill: var(--bg-surface);
        stroke: var(--border-strong);
        stroke-width: 1.6;
      }
      .node--sel .node__body {
        stroke-width: 2.5;
      }
      .node[data-kind="initiator"] .node__body,
      .node[data-kind="driver"] .node__body,
      .node[data-kind="group"] .node__body {
        stroke: var(--brand);
      }
      .node[data-kind="participant"] .node__body {
        stroke: var(--color-info, #38bdf8);
      }
      .node[data-kind="simulator"] .node__body {
        stroke: var(--color-success);
      }
      .node[data-kind="host"] .node__body {
        stroke: var(--color-warning);
        stroke-dasharray: 5 4;
      }
      .node[data-kind="clients"] .node__body {
        stroke: var(--text-secondary);
        stroke-dasharray: 5 4;
      }
      .node[data-status="unresolved"] .node__body {
        stroke: var(--color-danger);
        stroke-dasharray: 5 4;
      }
      .node[data-status="down"] .node__body {
        fill: var(--bg-base);
      }
      .node__pulse {
        fill: none;
        stroke: var(--brand);
        stroke-width: 2;
        opacity: 0;
        animation: tm3-ping 1.4s ease-out infinite;
        transform-origin: center;
        transform-box: fill-box;
      }
      @keyframes tm3-ping {
        0% {
          opacity: 0.55;
          transform: scale(1);
        }
        100% {
          opacity: 0;
          transform: scale(1.7);
        }
      }
      .node__aura {
        fill: none;
        stroke-width: 2;
      }
      .node[data-health="warn"] .node__aura {
        stroke: var(--color-warning);
      }
      .node[data-health="down"] .node__aura {
        stroke: var(--color-danger);
        stroke-dasharray: 4 4;
      }
      .node__statusdot {
        fill: var(--text-muted);
        stroke: var(--bg-surface);
        stroke-width: 1.5;
      }
      .node[data-status="up"] .node__statusdot {
        fill: var(--color-success);
      }
      .node[data-status="degraded"] .node__statusdot,
      .node[data-status="external"] .node__statusdot {
        fill: var(--color-warning);
      }
      .node[data-status="down"] .node__statusdot,
      .node[data-status="unresolved"] .node__statusdot {
        fill: var(--color-danger);
      }
      .node__ico {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 22px;
        height: 22px;
        color: var(--text-secondary);
      }
      .node--active .node__ico,
      .node--sel .node__ico {
        color: var(--brand);
      }
      .node__label {
        fill: var(--text-primary);
        font-size: 12.5px;
        font-weight: 600;
        paint-order: stroke;
        stroke: var(--bg-base);
        stroke-width: 3px;
        stroke-linejoin: round;
      }
      .node__sub {
        fill: var(--text-muted);
        font-size: 10px;
        font-family: var(--font-mono);
        paint-order: stroke;
        stroke: var(--bg-base);
        stroke-width: 3px;
        stroke-linejoin: round;
      }
      .node__stat {
        fill: var(--text-muted);
        font-size: 11px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        paint-order: stroke;
        stroke: var(--bg-base);
        stroke-width: 3px;
        stroke-linejoin: round;
      }
      .node__stat--hot {
        fill: var(--brand);
      }
      .trend {
        font-size: 9px;
      }
      .trend--up {
        fill: var(--color-success);
      }
      .trend--down {
        fill: var(--color-danger);
      }
      .node__flag {
        fill: var(--color-danger);
        font-size: 9px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }
      .node__flag--fleet {
        fill: var(--brand);
      }

      @keyframes tm3-pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.25;
        }
      }
      .pulse {
        animation: tm3-pulse 0.9s ease-in-out infinite;
      }

      /* motion hygiene — respect the OS setting */
      @media (prefers-reduced-motion: reduce) {
        .edge__flow {
          animation: none;
        }
        .edge__packet {
          display: none;
        }
        .node__pulse {
          display: none;
        }
        .sweep {
          display: none;
        }
        .pulse {
          animation: none;
        }
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
        border-radius: 50%;
        flex: none;
        border: 2px solid transparent;
        background: transparent;
      }
      .lg--initiator {
        border-color: var(--brand);
      }
      .lg--participant {
        border-color: var(--color-info, #38bdf8);
      }
      .lg--simulator {
        border-color: var(--color-success);
      }
      .lg--host {
        border-color: var(--color-warning);
        border-style: dashed;
      }
      .lg--clients {
        border-color: var(--text-secondary);
        border-style: dashed;
      }
      .lg--bad {
        border-color: var(--color-danger);
        border-style: dashed;
      }
      .lg--flow {
        background: var(--brand);
        border: none;
      }
      .legend__flow {
        margin-left: auto;
      }
    `,
  ],
})
export class TopologyMap3Component implements OnInit, OnDestroy {
  private readonly recorder = inject(ChronoscopeRecorderService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  /** Shared graph feed (SSE with poll fallback) — one loop for all maps. */
  readonly stream = inject(NetworkGraphStreamService);

  // template helpers (pure, from the model)
  readonly kindIcon = kindIcon;
  readonly nodeSub = nodeSub;

  // tape (owned by the recorder)
  readonly frames = this.recorder.frames;
  readonly events = this.recorder.events;
  readonly source = this.recorder.source;
  readonly replayName = this.recorder.replayName;

  // view state
  /** Index into frames while replaying; null = tracking the right edge. */
  readonly viewIndex = signal<number | null>(null);
  /** Honest connection state — flips Live to Stale (shared feed's). */
  readonly pollHealth = this.stream.pollHealth;
  readonly stale = this.stream.stale;
  readonly pollSecs = POLL_MS / 1000;
  /** Recording lives in the root recorder; this only re-anchors a replay view
   *  when the rolling buffer drops old frames underneath it. */
  private lastTrims = this.recorder.trims();
  private readonly anchorFx = effect(() => {
    const t = this.recorder.trims();
    const d = t - this.lastTrims;
    this.lastTrims = t;
    if (d > 0 && this.viewIndex() !== null) {
      this.viewIndex.update((v) => (v === null ? null : Math.max(0, v - d)));
    }
  });
  /** Tab hidden — skip packet/pulse animations nobody can see. */
  readonly docHidden = signal(
    typeof document !== "undefined" && document.hidden,
  );
  @HostListener("document:visibilitychange")
  onVisibility(): void {
    this.docHidden.set(document.hidden);
  }
  /** Freeze packet/pulse motion when the live edge is showing dead data or
   *  the tab is hidden. Scrubbing history stays animated — deliberate replay. */
  readonly frozen = computed(
    () =>
      this.docHidden() ||
      (this.stale() && this.source() === "live" && this.isLive()),
  );
  readonly selected = signal<string | null>(null);
  readonly playing = signal(false);
  readonly speed = signal(2); // replay speed ×realtime
  readonly importError = signal<string | null>(null);

  private playTimer: ReturnType<typeof setInterval> | null = null;

  // -- frame selection ----------------------------------------------------
  readonly isLive = computed(() => this.viewIndex() === null);
  readonly displayIndex = computed(() => {
    const len = this.frames().length;
    if (!len) return -1;
    const v = this.viewIndex();
    return v === null ? len - 1 : Math.min(v, len - 1);
  });
  readonly frame = computed<Frame | null>(
    () => this.frames()[this.displayIndex()] ?? null,
  );
  readonly prevFrame = computed<Frame | null>(
    () => this.frames()[this.displayIndex() - 1] ?? null,
  );
  readonly graph = computed(() => this.frame()?.g ?? null);
  /** Real seconds behind the newest frame (timestamps, not frame counts —
   *  the tape can hold gaps from time spent on other pages). */
  readonly behindSecs = computed(() => {
    const fr = this.frames();
    const last = fr[fr.length - 1];
    const cur = fr[this.displayIndex()];
    return last && cur ? Math.max(0, Math.round((last.at - cur.at) / 1000)) : 0;
  });
  private readonly bufferSecs = computed(() => {
    const fr = this.frames();
    return fr.length > 1
      ? Math.max(0, Math.round((fr[fr.length - 1].at - fr[0].at) / 1000))
      : 0;
  });
  readonly bufferLabel = computed(() => {
    const secs = this.bufferSecs();
    if (secs < 60) return secs + "s of history buffered";
    return Math.round(secs / 60) + "m of history buffered";
  });
  readonly bufferSpanLabel = computed(() => {
    const secs = this.bufferSecs();
    return secs < 60 ? secs + "s" : Math.round(secs / 60) + "m";
  });

  // -- KPIs (of the displayed frame — they time-travel too) ---------------
  readonly totalRate = computed(() => this.frame()?.tps ?? 0);
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

  // -- focus / inspector ---------------------------------------------------
  readonly selectedNode = computed(() => {
    const id = this.selected();
    return id ? (this.graph()?.nodes.find((n) => n.id === id) ?? null) : null;
  });
  readonly selectedLabel = computed(() => this.selectedNode()?.label ?? "");
  readonly selectedRate = computed(() => {
    const id = this.selected();
    return id ? (this.frame()?.rates.get(id) ?? 0) : 0;
  });
  /** Rate history up to the displayed frame (fed to the inspector sparkline). */
  readonly sparkVals = computed<number[]>(() => {
    const id = this.selected();
    if (!id) return [];
    const fr = this.frames();
    const end = this.displayIndex();
    if (end < 0) return [];
    const start = Math.max(0, end - 59);
    const vals: number[] = [];
    for (let i = start; i <= end; i++) vals.push(fr[i].rates.get(id) || 0);
    return vals;
  });
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

  // -- scrubber feed ---------------------------------------------------------
  readonly eventMarks = computed<StripMark[]>(() => {
    const fr = this.frames();
    if (fr.length < 2) return [];
    const idxBySeq = new Map(fr.map((f, i) => [f.seq, i]));
    const marks: StripMark[] = [];
    for (const e of this.events()) {
      const i = idxBySeq.get(e.seq);
      if (i === undefined) continue; // trimmed out of the buffer
      marks.push({
        key: e.key,
        x: (i / (fr.length - 1)) * STRIP_W,
        tone: e.tone,
        text: e.text + " · " + e.time,
      });
    }
    return marks;
  });
  readonly playheadX = computed(() => {
    const len = this.frames().length;
    return len > 1 ? (this.displayIndex() / (len - 1)) * STRIP_W : 0;
  });
  readonly stripLine = computed(() =>
    seriesPoints(
      this.frames().map((f) => f.tps),
      STRIP_W,
      44,
    ),
  );
  readonly stripArea = computed(() => {
    const pts = this.stripLine();
    if (!pts) return "";
    return "0,44 " + pts + " " + STRIP_W + ",44";
  });

  readonly layout = computed(() =>
    computeOrbitalLayout(this.frame(), this.prevFrame()),
  );

  // -- shareable URL state: ?focus=<node> (the playhead points into a
  //    client-local tape, so time deep-links would lie across users; sharing
  //    a moment is what replay export is for). ----------------------------
  private urlReady = false;
  private readonly urlFx = effect(() => {
    const focus = this.selected();
    if (!this.urlReady) return;
    this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { focus: focus || null },
      queryParamsHandling: "merge",
      replaceUrl: true,
    });
  });

  ngOnInit() {
    const focus = this.route.snapshot.queryParamMap.get("focus");
    if (focus) this.selected.set(focus);
    this.urlReady = true;
    this.stream.acquire();
  }
  ngOnDestroy() {
    this.stream.release();
    this.stopPlayback();
  }

  /** Manual refresh — pulls one fresh frame; the root recorder tapes it. */
  refresh() {
    if (this.source() === "live") this.stream.refreshOnce();
  }

  // -- replay import / export ----------------------------------------------
  exportReplay() {
    this.recorder.exportReplay();
  }
  async onImportFile(ev: Event) {
    const inp = ev.target as HTMLInputElement;
    const file = inp.files?.[0];
    inp.value = "";
    if (!file) return;
    this.stopPlayback();
    try {
      await this.recorder.importReplay(file);
      this.viewIndex.set(0); // start of the tape
      this.selected.set(null);
      this.importError.set(null);
    } catch (e) {
      this.importError.set(e instanceof Error ? e.message : "Import failed");
    }
  }
  /** Eject the loaded replay and resume recording live polls. */
  backToLive() {
    this.stopPlayback();
    this.recorder.reset();
    this.viewIndex.set(null);
    this.selected.set(null);
    this.importError.set(null);
    this.refresh();
  }

  // -- transport -------------------------------------------------------------
  goLive() {
    this.viewIndex.set(null);
  }
  onScrub(t: number) {
    this.stopPlayback();
    const len = this.frames().length;
    if (len < 2) return;
    const idx = Math.round(t * (len - 1));
    this.viewIndex.set(idx >= len - 1 ? null : idx);
  }
  stepFrame(d: number) {
    this.stopPlayback();
    const len = this.frames().length;
    if (len < 2) return;
    const idx = Math.min(len - 1, Math.max(0, this.displayIndex() + d));
    this.viewIndex.set(idx >= len - 1 ? null : idx);
  }
  togglePlay() {
    if (this.playing()) {
      this.stopPlayback();
      return;
    }
    if (this.frames().length < 2) return;
    if (this.isLive()) this.viewIndex.set(0); // play the whole buffer
    this.playing.set(true);
    this.startPlayTimer();
  }
  cycleSpeed() {
    this.speed.update((s) => (s >= 8 ? 1 : s * 2));
    if (this.playing()) this.startPlayTimer();
  }
  private startPlayTimer() {
    if (this.playTimer) clearInterval(this.playTimer);
    this.playTimer = setInterval(() => {
      const len = this.frames().length;
      const cur = this.displayIndex();
      if (cur >= len - 1) {
        this.stopPlayback();
        this.viewIndex.set(null); // caught up — back to the right edge
        return;
      }
      this.viewIndex.set(cur + 1 >= len - 1 ? len - 1 : cur + 1);
    }, POLL_MS / this.speed());
  }
  stopPlayback() {
    this.playing.set(false);
    if (this.playTimer) {
      clearInterval(this.playTimer);
      this.playTimer = null;
    }
  }
  jumpTo(e: Evt) {
    this.stopPlayback();
    const fr = this.frames();
    const i = fr.findIndex((f) => f.seq === e.seq);
    this.viewIndex.set(i >= 0 ? i : 0);
    this.selected.set(e.nodeId || null);
  }

  @HostListener("document:keydown", ["$event"])
  onKey(ev: KeyboardEvent) {
    const t = ev.target as HTMLElement | null;
    if (
      t &&
      (t.tagName === "INPUT" ||
        t.tagName === "TEXTAREA" ||
        t.tagName === "SELECT" ||
        t.isContentEditable)
    )
      return;
    switch (ev.key) {
      case "ArrowLeft":
        ev.preventDefault();
        this.stepFrame(-1);
        break;
      case "ArrowRight":
        ev.preventDefault();
        this.stepFrame(1);
        break;
      case " ":
        ev.preventDefault();
        this.togglePlay();
        break;
      case "l":
      case "L":
        this.stopPlayback();
        this.goLive();
        break;
      case "Escape":
        this.clearFocus();
        break;
    }
  }

  // -- focus -------------------------------------------------------------------
  selectNode(id: string) {
    this.selected.update((c) => (c === id ? null : id));
  }
  clearFocus() {
    this.selected.set(null);
  }
  nodeOpacity(n: RNode): number {
    const s = this.selected();
    if (!s) return 1;
    if (n.id === s) return 1;
    return this.adjacency().get(s)?.has(n.id) ? 1 : 0.28;
  }
  edgeOpacity(e: REdge): number {
    const s = this.selected();
    if (!s) return 1;
    return e.source === s || e.target === s ? 1 : 0.15;
  }

  // -- scope rendering helpers ----------------------------------------------
  pathId(e: REdge): string {
    return "tm3p-" + e.id.replace(/[^a-zA-Z0-9]/g, "-");
  }
  packetDur(e: REdge): string {
    const r = this.frame()?.rates.get(e.target) || 0;
    const secs = r > 50 ? 0.7 : r > 10 ? 1.0 : 1.4;
    return secs + "s";
  }
  /** Flow stroke grows with the live rate on the edge's busy end. */
  flowWidth(e: REdge): number {
    const fr = this.frame();
    const r = Math.max(
      fr?.rates.get(e.target) || 0,
      fr?.rates.get(e.source) || 0,
    );
    return +(1.4 + Math.min(3, r / 20)).toFixed(1);
  }
  nodeLabel(n: RNode): string {
    return n.label.length > 20 ? n.label.slice(0, 19) + "…" : n.label;
  }
  nodeStat(n: RNode): string {
    if (n.active && n.rate > 0) return n.rate + "/s";
    if (n.received > 0)
      return n.received >= 1000
        ? (n.received / 1000).toFixed(1) + "k"
        : "" + n.received;
    return "";
  }
}
