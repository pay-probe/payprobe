import {
  Component,
  OnInit,
  OnDestroy,
  inject,
  signal,
  computed,
  ChangeDetectionStrategy,
} from "@angular/core";
import { RouterLink } from "@angular/router";

import {
  Peer,
  PeersSnapshot,
  RunApiService,
} from "../run-monitor/run-api.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";
import { KpiCardComponent } from "../shared/ui/kpi-card.component";

/** Live Sessions — every peer currently connected to a listener PayProbe hosts:
 *  external clients dialing a simulator, or a participant-flow responder. One
 *  row per established socket, refreshed every 2s. Outbound (sockets the engine
 *  opens to hosts during a run) is reserved — shown as a placeholder for now. */
@Component({
  selector: "app-peers",
  standalone: true,
  imports: [
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    BadgeComponent,
    KpiCardComponent,
    RouterLink,
  ],
  template: `
    <div class="pe">
      <pp-page-header
        subtitle="Live connections — who is talking to the listeners you host"
      >
        <span class="live" [class.live--stale]="stale()">
          <span class="live__dot"></span>
          {{
            stale()
              ? "reconnecting — data " + (pollHealth.sinceOk() ?? "?") + "s old"
              : "live"
          }}
        </span>
        <pp-button
          variant="secondary"
          size="sm"
          icon="activity"
          (click)="reload()"
        >
          Refresh
        </pp-button>
      </pp-page-header>

      <div class="pe__body">
        <section class="kpis">
          <pp-kpi-card
            label="Inbound peers"
            [value]="counts().inbound"
            icon="send"
            caption="established client sockets"
          />
          <pp-kpi-card
            label="Distinct IPs"
            [value]="counts().peer_ips"
            icon="layers"
            caption="unique remote hosts"
          />
          <pp-kpi-card
            label="Listeners"
            [value]="counts().listeners"
            icon="cpu"
            caption="simulators + participants"
          />
          <pp-kpi-card
            label="Outbound peers"
            [value]="counts().outbound"
            icon="up"
            caption="engine → host (coming soon)"
          />
        </section>

        <!-- Inbound -->
        <section class="block">
          <header class="block__head">
            <h3>Inbound</h3>
            <span class="muted"
              >External clients connected to your listeners</span
            >
          </header>

          @if (loading() && !snapshot()) {
            <div class="state"><span class="muted">Loading…</span></div>
          } @else if (!inbound().length) {
            <div class="state state--empty">
              <span class="state__icon"
                ><pp-icon name="send" [size]="26"
              /></span>
              <h4>No inbound connections</h4>
              <p class="muted">
                When something dials one of your running simulators or
                participant responders, the live socket shows up here.
              </p>
              <a class="link" routerLink="/simulators">
                <pp-icon name="up" [size]="13" /> Go to Simulators
              </a>
            </div>
          } @else {
            <div class="tbl" role="table">
              <div class="tr tr--head" role="row">
                <span role="columnheader">Remote peer</span>
                <span role="columnheader">Listener</span>
                <span role="columnheader">Port</span>
                <span role="columnheader">Proto</span>
                <span role="columnheader">Msgs</span>
                <span role="columnheader">Connected</span>
                <span role="columnheader">Idle</span>
              </div>
              @for (p of inbound(); track p.source_id + p.peer) {
                <div class="tr" role="row">
                  <span class="mono peer" role="cell">
                    <span class="dot"></span>{{ p.peer }}
                  </span>
                  <span class="lbl" role="cell">
                    <a [routerLink]="listenerLink(p)">{{ p.label }}</a>
                    <pp-badge
                      [tone]="p.source === 'participant' ? 'info' : 'neutral'"
                    >
                      {{ p.source }}
                    </pp-badge>
                  </span>
                  <span class="mono" role="cell">{{
                    p.local_port ?? "—"
                  }}</span>
                  <span role="cell"
                    ><pp-badge tone="neutral">{{ p.protocol }}</pp-badge></span
                  >
                  <span class="num" role="cell">{{ p.messages }}</span>
                  <span class="muted" role="cell">{{
                    dur(p.connected_s)
                  }}</span>
                  <span class="muted" role="cell" [class.warn]="p.idle_s > 30">
                    {{ dur(p.idle_s) }}
                  </span>
                </div>
              }
            </div>
          }
        </section>

        <!-- Outbound (reserved) -->
        <section class="block">
          <header class="block__head">
            <h3>Outbound</h3>
            <span class="muted"
              >Sockets the engine opens to hosts during a run</span
            >
          </header>
          <div class="state state--soon">
            <span class="state__icon"><pp-icon name="up" [size]="26" /></span>
            <h4>Not wired yet</h4>
            <p class="muted">
              Outbound adapter connections live in the worker per-run. Reporting
              them here needs the worker to publish its open sockets back to the
              orchestrator — a follow-up to this view.
            </p>
          </div>
        </section>
      </div>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        background: var(--bg-base);
        color: var(--text-primary);
      }
      .pe {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .pe__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: var(--space-6);
        display: flex;
        flex-direction: column;
        gap: var(--space-5);
      }
      .muted {
        color: var(--text-muted);
        font-size: var(--text-sm);
      }
      .mono {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: var(--text-sm);
      }
      .live {
        display: inline-flex;
        align-items: center;
        gap: var(--space-1-5);
        font-size: var(--text-xs);
        font-weight: var(--weight-medium);
        color: var(--color-success);
      }
      .live--stale {
        color: var(--text-muted);
      }
      .live__dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: currentColor;
        box-shadow: 0 0 0 4px color-mix(in srgb, currentColor 22%, transparent);
      }

      .kpis {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: var(--space-4);
      }

      .block {
        display: flex;
        flex-direction: column;
        gap: var(--space-3);
      }
      .block__head {
        display: flex;
        align-items: baseline;
        gap: var(--space-3);
      }
      .block__head h3 {
        margin: 0;
        font-size: var(--text-base);
      }

      /* Table */
      .tbl {
        border: 1px solid var(--border);
        border-radius: var(--radius-xl);
        overflow: hidden;
        background: var(--bg-surface);
        box-shadow: var(--shadow-xs);
      }
      .tr {
        display: grid;
        grid-template-columns: 2fr 2fr 0.7fr 0.9fr 0.7fr 1fr 0.9fr;
        align-items: center;
        gap: var(--space-3);
        padding: var(--space-3) var(--space-4);
        border-top: 1px solid var(--border);
        font-size: var(--text-sm);
      }
      .tr--head {
        border-top: none;
        background: var(--bg-subtle);
        color: var(--text-muted);
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
      }
      .tr:not(.tr--head):hover {
        background: var(--bg-subtle);
      }
      .peer {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
        color: var(--text-primary);
      }
      .dot {
        flex: none;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--color-success);
        box-shadow: 0 0 0 3px
          color-mix(in srgb, var(--color-success) 22%, transparent);
      }
      .lbl {
        display: inline-flex;
        align-items: center;
        gap: var(--space-2);
        min-width: 0;
      }
      .lbl a {
        color: var(--brand);
        text-decoration: none;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .lbl a:hover {
        text-decoration: underline;
      }
      .num {
        font-variant-numeric: tabular-nums;
        font-weight: var(--weight-semibold);
      }
      .warn {
        color: var(--color-warning);
      }

      /* States */
      .state {
        padding: var(--space-6);
      }
      .state--empty,
      .state--soon {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        gap: var(--space-2);
        padding: var(--space-8) var(--space-6);
        border: 1px dashed var(--border-strong);
        border-radius: var(--radius-xl);
        background: var(--bg-surface);
      }
      .state--empty h4,
      .state--soon h4 {
        margin: 0;
        font-size: var(--text-base);
      }
      .state--empty p,
      .state--soon p {
        max-width: 48ch;
        margin: 0;
      }
      .state__icon {
        display: grid;
        place-items: center;
        width: 52px;
        height: 52px;
        border-radius: var(--radius-full);
        color: var(--brand);
        background: color-mix(in srgb, var(--brand) 12%, transparent);
        margin-bottom: var(--space-1);
      }
      .link {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: var(--text-sm);
        color: var(--brand);
        text-decoration: none;
      }
      .link:hover {
        text-decoration: underline;
      }

      @media (max-width: 860px) {
        .kpis {
          grid-template-columns: 1fr 1fr;
        }
        .tr {
          grid-template-columns: 1.6fr 1.4fr 0.8fr;
        }
        .tr span:nth-child(n + 4) {
          display: none;
        }
      }
    `,
  ],
})
export class PeersComponent implements OnInit, OnDestroy {
  private readonly api = inject(RunApiService);

  readonly snapshot = signal<PeersSnapshot | null>(null);
  readonly loading = signal(true);
  readonly pollHealth = new PollHealth();
  readonly stale = this.pollHealth.stale;
  private readonly poll = new PagePoll(2000, () => this.tick());

  readonly inbound = computed(() => this.snapshot()?.inbound ?? []);
  readonly counts = computed(
    () =>
      this.snapshot()?.counts ?? {
        inbound: 0,
        outbound: 0,
        listeners: 0,
        peer_ips: 0,
      },
  );

  ngOnInit() {
    this.poll.start(); // fires immediately, skips ticks while the tab is hidden
  }

  ngOnDestroy() {
    this.poll.stop();
    this.pollHealth.destroy();
  }

  reload() {
    this.loading.set(true);
    this.fetch(() => this.loading.set(false));
  }

  private tick() {
    this.fetch();
  }

  private fetch(done?: () => void) {
    this.api.peers().subscribe({
      next: (snap: PeersSnapshot) => {
        this.snapshot.set(snap);
        this.loading.set(false);
        this.pollHealth.markOk();
        done?.();
      },
      error: () => {
        this.pollHealth.markError();
        done?.();
      },
    });
  }

  /** Deep-link a row to the listener that hosts it. */
  listenerLink(p: Peer): unknown[] {
    return p.source === "participant"
      ? ["/participants"]
      : ["/simulators", p.source_id];
  }

  /** Compact duration: "8s", "3m 12s", "1h 04m". */
  dur(s: number): string {
    s = Math.max(0, Math.floor(s));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  }
}
