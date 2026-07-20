import {
  Component,
  OnInit,
  OnDestroy,
  inject,
  signal,
  computed,
  ChangeDetectionStrategy,
} from "@angular/core";

import { Router } from "@angular/router";

import { UiService } from "../shared/ui.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";
import { TopologiesService, TopologyRun } from "./topologies.service";
import {
  ParticipantsService,
  RunningParticipant,
} from "../participants/participants.service";
import { GroupsService } from "../groups/groups.service";
import { RunApiService, SavedSimulator } from "../run-monitor/run-api.service";
import { ConnectionsService } from "../connections/connections.service";
import { Connection } from "../connections/connection.models";
import { MapSwitcherComponent } from "./map-switcher.component";

/**
 * Network Control (route `/topology-map`, nav "Network Control") — the live
 * *control board* for the whole simulated network: networks + flows, routing
 * groups (with wiring links to their members), connections (enable/disable),
 * standalone listeners and simulators — each with start/stop / enable-disable
 * controls. Polls every few seconds.
 *
 * NOTE (ATLAS §12): distinct from the "Live Network Map" (topology-map2, the
 * visual diagram) and "Network Replay" (chronoscope). An earlier attempt to
 * merge this into Map 2 was reverted — Map 2 renders only the /network-graph
 * diagram and has none of these control sections, so they are different jobs.
 */
@Component({
  selector: "app-topology-map",
  standalone: true,
  imports: [
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    BadgeComponent,
    MapSwitcherComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <pp-page-header>
      <h1>Network Control</h1>
    </pp-page-header>

    <div class="tm">
      <!-- sticky control bar -->
      <div class="tm__bar">
        <app-map-switcher />
        <span class="tm__live" [class.tm__live--paused]="paused()">
          <span
            class="dot"
            [class.dot--ok]="!paused()"
            [class.pulse]="!paused()"
          ></span>
          {{ paused() ? "Paused" : "Live" }}
        </span>
        <span class="tm__sep"></span>
        <span class="muted sm">
          @if (paused()) {
            auto-refresh off
          } @else {
            auto-refreshes every 4s
          }
          @if (lastUpdated()) {
            · updated {{ lastUpdatedLabel() }}
          }
        </span>
        <span class="spacer"></span>
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

      <!-- hero health stats -->
      <div class="tm__metrics">
        <div
          class="metric"
          [attr.data-tone]="ratioTone(topologiesUp(), topo.topologies().length)"
        >
          <div class="metric__icon"><pp-icon name="flow" [size]="18" /></div>
          <div class="metric__body">
            <span class="metric__k">Topologies up</span>
            <span class="metric__v"
              >{{ topologiesUp()
              }}<span class="metric__of"
                >/ {{ topo.topologies().length }}</span
              ></span
            >
            <span class="meter"
              ><span
                class="meter__fill"
                [style.width.%]="
                  ratio(topologiesUp(), topo.topologies().length)
                "
              ></span
            ></span>
          </div>
        </div>
        <div class="metric" data-tone="info">
          <div class="metric__icon">
            <pp-icon name="standalone" [size]="18" />
          </div>
          <div class="metric__body">
            <span class="metric__k">Listeners ready</span>
            <span class="metric__v">{{ listenersReady() }}</span>
            <span class="metric__hint muted sm">bound &amp; accepting</span>
          </div>
        </div>
        <div class="metric" data-tone="info">
          <div class="metric__icon"><pp-icon name="layers" [size]="18" /></div>
          <div class="metric__body">
            <span class="metric__k">Connections</span>
            <span class="metric__v"
              >{{ connsEnabled()
              }}<span class="metric__of"
                >/ {{ conns.connections().length }}</span
              ></span
            >
            <span class="metric__hint muted sm"
              >{{ connsEnabled() }} enabled</span
            >
          </div>
        </div>
        <div
          class="metric"
          [attr.data-tone]="ratioTone(simsRunning(), sims().length)"
        >
          <div class="metric__icon"><pp-icon name="cpu" [size]="18" /></div>
          <div class="metric__body">
            <span class="metric__k">Simulators up</span>
            <span class="metric__v"
              >{{ simsRunning()
              }}<span class="metric__of">/ {{ sims().length }}</span></span
            >
            <span class="meter"
              ><span
                class="meter__fill"
                [style.width.%]="ratio(simsRunning(), sims().length)"
              ></span
            ></span>
          </div>
        </div>
      </div>

      <!-- topologies -->
      <section class="sect">
        <header class="sect__head">
          <pp-icon name="flow" [size]="16" />
          <h2>Networks</h2>
          <span class="count">{{ topo.topologies().length }}</span>
        </header>

        @for (t of topo.topologies(); track t.id) {
          <article class="topo" [attr.data-state]="topoState(t.id)">
            <div class="topo__head">
              <span class="topo__title">
                <span
                  class="statusdot"
                  [attr.data-state]="topoState(t.id)"
                ></span>
                <strong>{{ t.name }}</strong>
              </span>
              @if (runFor(t.id); as r) {
                @if (r.health?.ready) {
                  <pp-badge tone="success">running</pp-badge>
                } @else {
                  <pp-badge tone="warning">degraded</pp-badge>
                }
                <span class="ready muted sm"
                  >{{ r.health?.live }}/{{ r.health?.total }} ready</span
                >
                <span class="spacer"></span>
                <span class="muted sm mono runid">{{ r.id }}</span>
                <pp-button
                  variant="ghost"
                  size="sm"
                  icon="flow"
                  (click)="openCanvas(t.id)"
                  >Canvas</pp-button
                >
                <pp-button
                  variant="danger"
                  size="sm"
                  icon="x"
                  (click)="stopTopology(r.id)"
                  >Stop</pp-button
                >
              } @else {
                <pp-badge tone="neutral">stopped</pp-badge>
                <span class="spacer"></span>
                <pp-button
                  variant="ghost"
                  size="sm"
                  icon="flow"
                  (click)="openCanvas(t.id)"
                  >Canvas</pp-button
                >
                <pp-button
                  variant="primary"
                  size="sm"
                  icon="play"
                  (click)="startTopology(t.id)"
                  >Start</pp-button
                >
              }
            </div>

            <div class="chain">
              <span class="chain__src"
                ><pp-icon name="zap" [size]="13" /> driver</span
              >
              @for (p of t.participants ?? []; track p.flow_id) {
                <span class="chain__arrow"
                  ><pp-icon name="chevron" [size]="14"
                /></span>
                <div class="flowcol">
                  <span class="flowcol__name"
                    >{{ p.flow_id }}
                    <span class="muted">×{{ p.instances }}</span></span
                  >
                  @if (runFor(t.id); as r) {
                    @for (inst of instancesOf(r, p.flow_id); track inst.id) {
                      <span class="chip"
                        ><span
                          class="dot dot--ok"
                          [class.pulse]="isActivePort(inst.port)"
                        ></span
                        >{{ inst.endpoint || ":" + inst.port }}</span
                      >
                    } @empty {
                      <span class="chip chip--bad"
                        ><span class="dot dot--bad"></span>no live
                        instance</span
                      >
                    }
                  } @else {
                    <span class="chip chip--off"
                      ><span class="dot dot--off"></span>not running</span
                    >
                  }
                </div>
              }
            </div>
          </article>
        } @empty {
          <p class="muted empty">No networks defined yet.</p>
        }
      </section>

      <!-- routing groups -->
      <section class="sect">
        <header class="sect__head">
          <pp-icon name="activity" [size]="16" />
          <h2>Routing groups</h2>
          <span class="count">{{ groups.groups().length }}</span>
        </header>
        <div class="grid">
          @for (g of groups.groups(); track g.id) {
            <div class="gcard">
              <div class="gcard__head">
                <span class="gcard__title">
                  <strong>{{ g.name }}</strong>
                  @if (familyOf(g); as fam) {
                    <span class="gtag">{{ fam }}</span>
                  }
                </span>
                <pp-badge tone="info">{{
                  g.selection?.policy || "round_robin"
                }}</pp-badge>
              </div>
              <svg
                [attr.viewBox]="'0 0 440 ' + svgH(g.members?.length || 1)"
                class="wire"
              >
                <rect
                  x="8"
                  [attr.y]="svgMid(g.members?.length || 1) - 16"
                  width="120"
                  height="32"
                  rx="8"
                  class="wire__group"
                />
                <text
                  x="68"
                  [attr.y]="svgMid(g.members?.length || 1) + 4"
                  text-anchor="middle"
                  class="wire__t"
                >
                  {{ g.name }}
                </text>
                @for (
                  m of g.members ?? [];
                  track m.connection;
                  let i = $index
                ) {
                  <line
                    x1="128"
                    [attr.y1]="svgMid(g.members?.length || 1)"
                    x2="280"
                    [attr.y2]="rowY(i) + 16"
                    [class.flow]="isActive(m.connection)"
                    [attr.stroke]="statusColor(memberStatus(m.connection))"
                    [attr.stroke-dasharray]="
                      isActive(m.connection)
                        ? '6 6'
                        : memberStatus(m.connection) === 'ok'
                          ? '0'
                          : '5 3'
                    "
                    stroke-width="1.5"
                  />
                  <rect
                    x="280"
                    [attr.y]="rowY(i)"
                    width="152"
                    height="32"
                    rx="8"
                    class="wire__node"
                    [attr.stroke]="statusColor(memberStatus(m.connection))"
                  />
                  <circle
                    cx="294"
                    [attr.cy]="rowY(i) + 16"
                    r="4"
                    [class.pulse]="isActive(m.connection)"
                    [attr.fill]="statusColor(memberStatus(m.connection))"
                  />
                  <text x="306" [attr.y]="rowY(i) + 14" class="wire__t">
                    {{ m.connection }}
                  </text>
                  <text x="306" [attr.y]="rowY(i) + 26" class="wire__ts">
                    {{ memberStatus(m.connection) }} · w{{ m.weight }}
                  </text>
                }
              </svg>
            </div>
          } @empty {
            <p class="muted empty">No groups defined.</p>
          }
        </div>
      </section>

      <!-- connections -->
      <section class="sect">
        <header class="sect__head">
          <pp-icon name="layers" [size]="16" />
          <h2>Connections</h2>
          <span class="count">{{ conns.connections().length }}</span>
        </header>
        <div class="tiles">
          @for (c of conns.connections(); track c.name) {
            <div class="tile" [attr.data-state]="c.disabled ? 'off' : 'ok'">
              <span
                class="statusdot"
                [attr.data-state]="c.disabled ? 'off' : 'ok'"
              ></span>
              <div class="tile__body">
                <span class="tile__nameline">
                  <span class="tile__name">{{ c.name }}</span>
                  <span class="gtag">{{ connFamily(c) }}</span>
                </span>
                <span class="tile__meta muted sm mono"
                  >{{ c.mode === "inbound" ? "in" : "out" }} ·
                  {{ c.host || "—" }}:{{ c.port }}</span
                >
              </div>
              @if (c.disabled) {
                <button class="act" (click)="toggleConn(c)">
                  <pp-icon name="play" [size]="13" /> Enable
                </button>
              } @else {
                <button class="act act--warn" (click)="toggleConn(c)">
                  <pp-icon name="x" [size]="13" /> Disable
                </button>
              }
            </div>
          } @empty {
            <p class="muted empty">No connections defined.</p>
          }
        </div>
      </section>

      <!-- standalone listeners -->
      @if (standalone().length) {
        <section class="sect">
          <header class="sect__head">
            <pp-icon name="standalone" [size]="16" />
            <h2>Standalone listeners</h2>
            <span class="count">{{ standalone().length }}</span>
          </header>
          <div class="tiles">
            @for (s of standalone(); track s.id) {
              <div class="tile" data-state="ok">
                <span
                  class="statusdot"
                  data-state="ok"
                  [class.pulse]="isActivePort(s.port)"
                ></span>
                <div class="tile__body">
                  <span class="tile__name">{{ s.flow_id }}</span>
                  <span class="tile__meta muted sm mono">{{
                    s.endpoint || ":" + s.port
                  }}</span>
                </div>
                <button class="act act--warn" (click)="stopFlow(s.id)">
                  <pp-icon name="x" [size]="13" /> Stop
                </button>
              </div>
            }
          </div>
        </section>
      }

      <!-- simulators -->
      <section class="sect">
        <header class="sect__head">
          <pp-icon name="cpu" [size]="16" />
          <h2>Simulators</h2>
          <span class="count">{{ sims().length }}</span>
        </header>
        <div class="tiles">
          @for (s of sims(); track s.id) {
            <div class="tile" [attr.data-state]="s.running ? 'ok' : 'off'">
              <span
                class="statusdot"
                [attr.data-state]="s.running ? 'ok' : 'off'"
                [class.pulse]="isActivePort(s.port)"
              ></span>
              <div class="tile__body">
                <span class="tile__nameline">
                  <span class="tile__name">{{ s.label || s.id }}</span>
                  @if (s.protocol) {
                    <span class="gtag">{{ s.protocol }}</span>
                  }
                </span>
                @if (s.running) {
                  <span class="tile__meta muted sm mono">:{{ s.port }}</span>
                } @else {
                  <span class="tile__meta muted sm">stopped</span>
                }
              </div>
              @if (s.running) {
                <button class="act act--warn" (click)="stopSim(s.id)">
                  <pp-icon name="x" [size]="13" /> Stop
                </button>
              } @else {
                <button class="act" (click)="startSim(s.id)">
                  <pp-icon name="play" [size]="13" /> Start
                </button>
              }
            </div>
          } @empty {
            <p class="muted empty">No simulators.</p>
          }
        </div>
      </section>

      <div class="legend">
        <span><span class="statusdot" data-state="ok"></span> up</span>
        <span
          ><span class="statusdot" data-state="degraded"></span> degraded</span
        >
        <span><span class="statusdot" data-state="down"></span> down</span>
        <span
          ><span class="statusdot" data-state="off"></span> stopped /
          disabled</span
        >
        <span class="legend__pulse"
          ><span class="dot dot--ok pulse"></span> live traffic</span
        >
      </div>
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
      .tm {
        margin-top: var(--space-4);
        max-width: 980px;
      }
      .muted {
        color: var(--text-muted);
      }
      .sm {
        font-size: var(--text-sm);
      }
      .mono {
        font-family: var(--font-mono);
      }
      .spacer {
        flex: 1;
      }

      /* -- sticky control bar -- */
      .tm__bar {
        position: sticky;
        top: 0;
        z-index: var(--z-sticky);
        display: flex;
        align-items: center;
        gap: var(--space-3);
        padding: var(--space-3) var(--space-4);
        margin-bottom: var(--space-5);
        background: color-mix(in srgb, var(--bg-surface) 88%, transparent);
        backdrop-filter: blur(8px);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        box-shadow: var(--shadow-sm);
      }
      .tm__live {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
        color: var(--color-success);
      }
      .tm__live--paused {
        color: var(--text-muted);
      }
      .tm__sep {
        width: 1px;
        height: 16px;
        background: var(--border);
      }

      /* -- hero metrics -- */
      .tm__metrics {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
        gap: var(--space-3);
        margin-bottom: var(--space-6);
      }
      .metric {
        display: flex;
        gap: var(--space-3);
        align-items: flex-start;
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-4);
        box-shadow: var(--shadow-xs);
        position: relative;
        overflow: hidden;
      }
      .metric::before {
        content: "";
        position: absolute;
        inset: 0 auto 0 0;
        width: 3px;
        background: var(--border-strong);
      }
      .metric[data-tone="ok"]::before {
        background: var(--color-success);
      }
      .metric[data-tone="warn"]::before {
        background: var(--color-warning);
      }
      .metric[data-tone="down"]::before {
        background: var(--color-danger);
      }
      .metric[data-tone="info"]::before {
        background: var(--brand);
      }
      .metric__icon {
        display: grid;
        place-items: center;
        width: 34px;
        height: 34px;
        flex: none;
        border-radius: var(--radius-md);
        background: var(--bg-subtle);
        color: var(--text-secondary);
      }
      .metric__body {
        display: flex;
        flex-direction: column;
        gap: 3px;
        min-width: 0;
      }
      .metric__k {
        font-size: var(--text-sm);
        color: var(--text-muted);
      }
      .metric__v {
        font-size: var(--text-2xl);
        font-weight: var(--weight-semibold);
        line-height: 1;
      }
      .metric__of {
        font-size: var(--text-md);
        font-weight: var(--weight-regular);
        color: var(--text-muted);
        margin-left: 4px;
      }
      .metric__hint {
        margin-top: 2px;
      }
      .meter {
        display: block;
        height: 4px;
        border-radius: var(--radius-full);
        background: var(--bg-subtle);
        margin-top: var(--space-2);
        overflow: hidden;
      }
      .meter__fill {
        display: block;
        height: 100%;
        border-radius: inherit;
        background: var(--color-success);
        transition: width var(--duration-base) var(--ease-out);
      }
      .metric[data-tone="warn"] .meter__fill {
        background: var(--color-warning);
      }
      .metric[data-tone="down"] .meter__fill {
        background: var(--color-danger);
      }

      /* -- section -- */
      .sect {
        margin-bottom: var(--space-7);
      }
      .sect__head {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        margin-bottom: var(--space-3);
        color: var(--text-secondary);
      }
      .sect__head h2 {
        margin: 0;
        font-size: var(--text-md);
        font-weight: var(--weight-semibold);
        color: var(--text-primary);
      }
      .count {
        font-size: var(--text-xs);
        font-weight: var(--weight-semibold);
        color: var(--text-muted);
        background: var(--bg-subtle);
        border-radius: var(--radius-full);
        padding: 1px var(--space-2);
        min-width: 18px;
        text-align: center;
      }
      .empty {
        padding: var(--space-3) 0;
      }

      /* -- topology card -- */
      .topo {
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-4) var(--space-5);
        margin-bottom: var(--space-3);
        box-shadow: var(--shadow-xs);
        border-left: 3px solid var(--border-strong);
      }
      .topo[data-state="ok"] {
        border-left-color: var(--color-success);
      }
      .topo[data-state="degraded"] {
        border-left-color: var(--color-warning);
      }
      .topo[data-state="off"] {
        border-left-color: var(--border-strong);
      }
      .topo__head {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        margin-bottom: var(--space-4);
      }
      .topo__title {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
      }
      .topo__title strong {
        font-size: var(--text-base);
      }
      .ready {
        white-space: nowrap;
      }
      .runid {
        white-space: nowrap;
      }

      /* -- flow chain -- */
      .chain {
        display: flex;
        align-items: flex-start;
        gap: var(--space-2);
        flex-wrap: wrap;
      }
      .chain__src {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 5px var(--space-2);
        border: 1px solid var(--brand);
        border-radius: var(--radius-md);
        color: var(--brand);
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
      }
      .chain__arrow {
        display: inline-flex;
        align-items: center;
        color: var(--text-muted);
        align-self: center;
      }
      .flowcol {
        display: flex;
        flex-direction: column;
        gap: 5px;
      }
      .flowcol__name {
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
      }
      .chip {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        padding: 5px var(--space-2);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        font-size: var(--text-sm);
        white-space: nowrap;
        background: var(--bg-base);
      }
      .chip--off {
        color: var(--text-muted);
      }
      .chip--bad {
        color: var(--color-danger);
        border-color: currentColor;
      }

      /* -- routing group cards -- */
      .grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
        gap: var(--space-3);
      }
      .gcard {
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-3) var(--space-4);
        box-shadow: var(--shadow-xs);
      }
      .gcard__head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--space-2);
        margin-bottom: var(--space-2);
      }
      .gcard__title {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
        min-width: 0;
      }
      .gcard__head strong {
        font-size: var(--text-base);
      }
      .gtag {
        flex: none;
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: 0.03em;
        color: var(--text-muted);
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        padding: 1px var(--space-2);
        white-space: nowrap;
      }
      .wire {
        width: 100%;
        height: auto;
      }
      .wire__group {
        fill: var(--bg-subtle);
        stroke: var(--brand);
      }
      .wire__node {
        fill: var(--bg-surface);
      }
      .wire__t {
        font-size: 11px;
        fill: var(--text-primary);
      }
      .wire__ts {
        font-size: 9.5px;
        fill: var(--text-muted);
      }
      @keyframes tm-flow {
        to {
          stroke-dashoffset: -24;
        }
      }
      .wire line.flow {
        animation: tm-flow 0.6s linear infinite;
      }

      /* -- node tiles (connections / listeners / simulators) -- */
      .tiles {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
        gap: var(--space-2);
      }
      .tile {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        padding: var(--space-2) var(--space-3);
        box-shadow: var(--shadow-xs);
      }
      .tile[data-state="off"] {
        background: var(--bg-base);
      }
      .tile__body {
        display: flex;
        flex-direction: column;
        gap: 1px;
        min-width: 0;
        flex: 1;
      }
      .tile__nameline {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        min-width: 0;
      }
      .tile__name {
        font-size: var(--text-base);
        font-weight: var(--weight-medium);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .tile__meta {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .tile[data-state="off"] .tile__name {
        color: var(--text-secondary);
      }

      /* -- action button -- */
      .act {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        flex: none;
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
        padding: var(--space-1) var(--space-2);
        border-radius: var(--radius-sm);
        border: 1px solid var(--border);
        background: var(--bg-surface);
        color: var(--text-secondary);
        cursor: pointer;
        transition:
          background var(--duration-fast) var(--ease-out),
          color var(--duration-fast) var(--ease-out),
          border-color var(--duration-fast) var(--ease-out);
      }
      .act:hover {
        background: var(--bg-subtle);
        color: var(--text-primary);
        border-color: var(--border-strong);
      }
      .act--warn {
        color: var(--color-danger);
      }
      .act--warn:hover {
        background: color-mix(in srgb, var(--color-danger) 12%, transparent);
        color: var(--color-danger);
        border-color: var(--color-danger);
      }

      /* -- status dots -- */
      .statusdot {
        width: 9px;
        height: 9px;
        border-radius: 50%;
        flex: none;
        background: var(--text-muted);
        box-shadow: 0 0 0 3px transparent;
      }
      .statusdot[data-state="ok"] {
        background: var(--color-success);
        box-shadow: 0 0 0 3px
          color-mix(in srgb, var(--color-success) 18%, transparent);
      }
      .statusdot[data-state="degraded"] {
        background: var(--color-warning);
        box-shadow: 0 0 0 3px
          color-mix(in srgb, var(--color-warning) 18%, transparent);
      }
      .statusdot[data-state="down"] {
        background: var(--color-danger);
        box-shadow: 0 0 0 3px
          color-mix(in srgb, var(--color-danger) 18%, transparent);
      }
      .statusdot[data-state="off"] {
        background: var(--text-muted);
      }

      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        display: inline-block;
        background: var(--text-muted);
        flex: none;
      }
      .dot--ok {
        background: var(--color-success);
      }
      .dot--bad {
        background: var(--color-danger);
      }
      .dot--off {
        background: var(--text-muted);
      }

      @keyframes tm-pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.3;
        }
      }
      .wire circle.pulse {
        animation: tm-pulse 0.9s ease-in-out infinite;
      }
      .dot.pulse,
      .statusdot.pulse {
        animation: tm-pulse 0.9s ease-in-out infinite;
      }

      /* -- legend -- */
      .legend {
        display: flex;
        gap: var(--space-5);
        flex-wrap: wrap;
        margin-top: var(--space-4);
        padding-top: var(--space-4);
        border-top: 1px solid var(--border);
        font-size: var(--text-sm);
        color: var(--text-muted);
      }
      .legend span {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
      }
      .legend__pulse {
        margin-left: auto;
      }
    `,
  ],
})
export class TopologyMapComponent implements OnInit, OnDestroy {
  readonly topo = inject(TopologiesService);
  readonly participants = inject(ParticipantsService);
  readonly groups = inject(GroupsService);
  readonly conns = inject(ConnectionsService);
  private readonly runApi = inject(RunApiService);
  private readonly ui = inject(UiService);
  private readonly router = inject(Router);

  /** Deep-link into the authoring canvas for this network. */
  openCanvas(id: string): void {
    this.router.navigate(["/network-flows"], { queryParams: { id } });
  }

  readonly sims = signal<SavedSimulator[]>([]);

  /** Auto-refresh pause toggle + last-poll timestamp for the control bar. */
  readonly paused = signal(false);
  readonly lastUpdated = signal<Date | null>(null);
  private readonly clockTick = signal(0);

  /** Ports that saw new traffic since the last poll — drives the flow animation
   *  on the wiring links and the pulsing node dots. */
  readonly activePorts = signal<Set<number>>(new Set());
  private prevRx = new Map<number, number>();

  private timer: ReturnType<typeof setInterval> | null = null;
  private clock: ReturnType<typeof setInterval> | null = null;

  readonly topologiesUp = computed(
    () => this.topo.runs().filter((r) => r.health?.ready).length,
  );
  readonly listenersReady = computed(
    () => this.participants.running().filter((p) => p.port != null).length,
  );
  readonly connsEnabled = computed(
    () => this.conns.connections().filter((c) => !c.disabled).length,
  );
  readonly standalone = computed(() =>
    this.participants
      .running()
      .filter((p) => !p.owner || p.owner === "standalone"),
  );
  readonly simsRunning = computed(
    () => this.sims().filter((s) => s.running).length,
  );

  /** Relative "x s ago" label for the last poll; recomputes on the clock tick. */
  readonly lastUpdatedLabel = computed(() => {
    this.clockTick();
    const t = this.lastUpdated();
    if (!t) return "";
    const secs = Math.max(0, Math.round((Date.now() - t.getTime()) / 1000));
    if (secs < 5) return "just now";
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.round(secs / 60);
    return `${mins}m ago`;
  });

  ngOnInit(): void {
    this.refresh();
    this.timer = setInterval(() => {
      if (!this.paused()) this.refresh();
    }, 4000);
    this.clock = setInterval(() => this.clockTick.update((n) => n + 1), 1000);
  }

  ngOnDestroy(): void {
    if (this.timer) clearInterval(this.timer);
    if (this.clock) clearInterval(this.clock);
  }

  togglePause(): void {
    this.paused.update((p) => !p);
    if (!this.paused()) this.refresh();
  }

  refresh(): void {
    this.topo.reload();
    this.participants.reloadRunning();
    this.groups.reload();
    this.conns.reload();
    this.runApi.savedSimulators().subscribe({
      next: (s) => {
        this.sims.set(s);
        this.updateActivity();
      },
      error: () => this.sims.set([]),
    });
    this.lastUpdated.set(new Date());
  }

  // -- hero ratio helpers ----------------------------------------------------
  ratio(up: number, total: number): number {
    return total > 0 ? Math.round((up / total) * 100) : 0;
  }
  ratioTone(up: number, total: number): string {
    if (total === 0) return "info";
    if (up >= total) return "ok";
    if (up === 0) return "down";
    return "warn";
  }

  /** Compare per-port message counts to the previous poll; a rise = live
   *  traffic on that port this interval. */
  private updateActivity(): void {
    const cur = new Map<number, number>();
    const add = (port: number | null | undefined, rx: number | undefined) => {
      if (port == null) return;
      cur.set(port, (cur.get(port) || 0) + (rx || 0));
    };
    for (const s of this.sims()) add(s.port, s.received);
    for (const p of this.participants.running()) add(p.port, p.received);
    const active = new Set<number>();
    for (const [port, rx] of cur) {
      const prev = this.prevRx.get(port);
      if (prev != null && rx > prev) active.add(port);
    }
    this.prevRx = cur;
    this.activePorts.set(active);
  }

  isActivePort(port?: number | null): boolean {
    return port != null && this.activePorts().has(port);
  }
  isActive(connName: string): boolean {
    const c = this.connByName(connName);
    return !!c && this.isActivePort(c.port);
  }

  runFor(topologyId: string): TopologyRun | undefined {
    return this.topo.runFor(topologyId);
  }

  /** 'ok' | 'degraded' | 'off' for a topology's card accent + status dot. */
  topoState(topologyId: string): string {
    const r = this.runFor(topologyId);
    if (!r) return "off";
    return r.health?.ready ? "ok" : "degraded";
  }

  instancesOf(run: TopologyRun, flowId: string): RunningParticipant[] {
    return this.participants
      .running()
      .filter((p) => p.owner === run.id && p.flow_id === flowId);
  }

  // -- wiring SVG geometry ---------------------------------------------------
  rowY(i: number): number {
    return 8 + i * 36;
  }
  svgH(n: number): number {
    return Math.max(48, 16 + n * 36);
  }
  svgMid(n: number): number {
    return this.svgH(n) / 2;
  }

  // -- member / connection status -------------------------------------------
  private connByName(name: string): Connection | undefined {
    return this.conns.connections().find((c) => c.name === name);
  }

  /** Adapter family of a connection — also shown as the tile tag. */
  connFamily(c: Connection): string {
    const a = c.adapter as string | undefined;
    return a === "grpc" || a === "http" ? a : c.protocol || "iso8583";
  }

  /** A group's adapter family for the card tag — its stamped ``adapter_type``,
   *  or derived from its first resolvable member for legacy groups. */
  familyOf(g: {
    adapter_type?: string;
    members?: { connection: string }[];
  }): string | null {
    const t = g.adapter_type;
    if (t === "grpc" || t === "http" || t === "iso8583" || t === "header_echo")
      return t;
    for (const m of g.members ?? []) {
      const c = this.connByName(m.connection);
      if (c) return this.connFamily(c);
    }
    return null;
  }
  private portUp(port?: number): boolean {
    if (port == null) return false;
    return (
      this.sims().some((s) => s.running && s.port === port) ||
      this.participants.running().some((p) => p.port === port)
    );
  }
  /** 'ok' | 'down' | 'disabled' | 'unknown' for a group member connection. */
  memberStatus(connName: string): string {
    const c = this.connByName(connName);
    if (!c) return "unknown";
    if (c.disabled) return "disabled";
    return this.portUp(c.port) ? "ok" : "down";
  }
  statusColor(status: string): string {
    if (status === "ok") return "var(--color-success)";
    if (status === "down") return "var(--color-danger)";
    if (status === "disabled") return "var(--color-warning)";
    return "var(--text-muted)";
  }

  // -- controls --------------------------------------------------------------
  startTopology(id: string): void {
    this.topo.start(id).subscribe({
      next: () => this.ui.toast("success", "Topology started."),
      error: (e) =>
        this.ui.toast("error", e?.error?.detail ?? "Could not start topology."),
    });
  }
  stopTopology(runId: string): void {
    this.topo.stopRun(runId).subscribe({
      next: () => this.ui.toast("success", "Topology stopped."),
      error: () => this.ui.toast("error", "Could not stop topology."),
    });
  }
  stopFlow(pid: string): void {
    this.participants.stop(pid).subscribe({
      next: () => this.ui.toast("success", "Listener stopped."),
      error: () => this.ui.toast("error", "Could not stop listener."),
    });
  }
  startSim(id: string): void {
    this.runApi.startSavedSimulator(id).subscribe({
      next: () => {
        this.ui.toast("success", "Simulator started.");
        this.refresh();
      },
      error: (e) =>
        this.ui.toast(
          "error",
          e?.error?.detail ?? "Could not start simulator.",
        ),
    });
  }
  stopSim(id: string): void {
    this.runApi.stopSavedSimulator(id).subscribe({
      next: () => {
        this.ui.toast("success", "Simulator stopped.");
        this.refresh();
      },
      error: () => this.ui.toast("error", "Could not stop simulator."),
    });
  }
  toggleConn(c: Connection): void {
    const op = this.conns.setDisabled(c.name, !c.disabled);
    if (!op) return;
    op.subscribe({
      next: () =>
        this.ui.toast(
          "success",
          c.disabled ? "Connection enabled." : "Connection disabled.",
        ),
      error: () => this.ui.toast("error", "Could not update connection."),
    });
  }
}
