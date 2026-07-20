import {
  Component,
  OnDestroy,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";

import { LoadApiService, LoadWorker } from "./load-api.service";
import { UiService } from "../shared/ui.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { StaleChipComponent } from "../shared/ui/stale-chip.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

/** Load-worker fleet view.
 *
 * Standalone `python -m worker.load_worker` processes heartbeat into the load
 * bus while they hold a shard; this polls GET /load-workers and lets an operator
 * drain a worker (or the whole fleet). Workers are ephemeral and per-run, so an
 * empty table just means no external fleet is currently claiming shards. */
@Component({
  selector: "app-load-workers",
  standalone: true,
  imports: [PageHeaderComponent, StaleChipComponent],
  template: `
    <div class="lw">
      <pp-page-header>
        <div subtitle>
          <span class="muted"
            >Live <code class="mono">load_worker</code> fleet — workers
            currently holding a shard</span
          >
        </div>
        <pp-stale-chip [health]="pollHealth" />
        <span class="muted">{{ workers().length }} live</span>
        <button class="btn" (click)="reload()">Refresh</button>
        <button
          class="btn btn--danger"
          [disabled]="!workers().length"
          (click)="stopAll()"
        >
          Drain all
        </button>
      </pp-page-header>

      <div class="lw__body">
        @if (error()) {
          <p class="err">{{ error() }}</p>
        }

        @if (loading() && !workers().length) {
          <p class="muted">Loading…</p>
        } @else if (!workers().length) {
          <div class="empty">
            <p>No workers are currently claiming shards.</p>
            <p class="muted">
              Workers are ephemeral and per-run. Start a fleet against the same
              Redis with
              <code class="mono"
                >LOAD_RUN_ID=&lt;id&gt; python -m worker.load_worker</code
              >, then launch a load run — joined workers show up here within a
              few seconds. Loads at or below the in-process cap need no external
              workers.
            </p>
          </div>
        } @else {
          <table class="tbl">
            <thead>
              <tr>
                <th>Worker</th>
                <th>Host</th>
                <th>Run</th>
                <th>Shard</th>
                <th>Type</th>
                <th class="num">TPS</th>
                <th class="num">Sent</th>
                <th class="num">Errors</th>
                <th class="num">Conns</th>
                <th class="num">Memory</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              @for (w of workers(); track w.worker_id) {
                <tr [class.draining]="w.draining">
                  <td class="mono">{{ w.worker_id }}</td>
                  <td class="mono">
                    {{ w.host || "—" }}
                    @if (w.pid) {
                      <span class="muted">:{{ w.pid }}</span>
                    }
                  </td>
                  <td class="mono">{{ shortRun(w.run_id) }}</td>
                  <td class="num">
                    {{ w.shard_index ?? "—" }}
                    @if (w.shard_count) {
                      <span class="muted">/{{ w.shard_count }}</span>
                    }
                  </td>
                  <td>{{ w.type || "—" }}</td>
                  <td class="num">{{ fmt(w.tps) }}</td>
                  <td class="num">{{ w.sent ?? 0 }}</td>
                  <td class="num" [class.bad]="(w.errors ?? 0) > 0">
                    {{ w.errors ?? 0 }}
                  </td>
                  <td class="num">{{ w.connections ?? 0 }}</td>
                  <td class="num mono">{{ fmtBytes(w.rss_bytes) }}</td>
                  <td>
                    @if (w.draining) {
                      <span class="pill pill--warn">draining</span>
                    } @else {
                      <span class="pill">{{ w.status || "running" }}</span>
                    }
                  </td>
                  <td class="actions">
                    <button
                      class="btn btn--sm btn--danger"
                      [disabled]="w.draining"
                      (click)="drain(w)"
                    >
                      Drain
                    </button>
                  </td>
                </tr>
              }
            </tbody>
          </table>
          <p class="muted note">
            Drain signals a worker to finish in-flight work and exit — it does
            not kill the OS process. Manage the actual processes (scale,
            restart, kill) through your orchestrator (k8s / Docker).
          </p>
        }
      </div>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .lw {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .lw__bar {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 12px 16px;
        border-bottom: 1px solid var(--color-border);
      }
      .lw__bar h1 {
        margin: 0;
        font-size: 16px;
      }
      .sp {
        flex: 1 1 auto;
      }
      .lw__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 14px;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
      }
      .note {
        font-size: 12px;
      }
      .mono {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 11px;
      }
      .err {
        color: var(--color-danger, #f85149);
        font-size: 13px;
      }
      .empty {
        border: 1px dashed var(--color-border);
        border-radius: 10px;
        padding: 20px;
      }
      .empty p {
        margin: 0 0 8px;
      }
      .tbl {
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
      }
      .tbl th,
      .tbl td {
        text-align: left;
        padding: 7px 10px;
        border-bottom: 1px solid var(--color-border);
      }
      .tbl th.num,
      .tbl td.num {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .tbl tr.draining {
        opacity: 0.6;
      }
      .tbl td.bad {
        color: var(--color-danger, #f85149);
      }
      .actions {
        text-align: right;
      }
      .pill {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 11px;
        border: 1px solid var(--color-border);
        color: var(--color-text-muted, var(--color-text));
      }
      .pill--warn {
        color: #b08800;
        border-color: #b08800;
      }
      .btn {
        padding: 7px 12px;
        border-radius: 7px;
        border: 1px solid var(--color-border);
        background: var(--color-bg);
        color: var(--color-text);
        cursor: pointer;
        font: inherit;
        font-size: 13px;
      }
      .btn:hover {
        background: var(--color-node);
      }
      .btn:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }
      .btn--sm {
        padding: 4px 9px;
        font-size: 12px;
      }
      .btn--danger {
        color: var(--color-danger, #f85149);
        border-color: var(--color-danger, #f85149);
        background: transparent;
      }
    `,
  ],
})
export class WorkersComponent implements OnInit, OnDestroy {
  private readonly api = inject(LoadApiService);
  private readonly ui = inject(UiService);

  readonly workers = signal<LoadWorker[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly pollHealth = new PollHealth();
  private readonly poll = new PagePoll(3000, () => this.reload(true));

  ngOnInit(): void {
    // light polling — presence is cheap and the fleet changes as workers join,
    // drain, and exit. Fires immediately; skips ticks while the tab is hidden.
    this.poll.start();
  }

  ngOnDestroy(): void {
    this.poll.stop();
    this.pollHealth.destroy();
  }

  reload(quiet = false): void {
    if (!quiet) this.loading.set(true);
    this.api.workers().subscribe({
      next: (list) => {
        this.workers.set(list ?? []);
        this.loading.set(false);
        this.error.set(null);
        this.pollHealth.markOk();
      },
      error: (e) => {
        this.loading.set(false);
        this.error.set(
          e?.error?.detail ??
            "Could not load workers — is the orchestrator running?",
        );
        this.pollHealth.markError();
      },
    });
  }

  async drain(w: LoadWorker): Promise<void> {
    const ok = await this.ui.confirm(
      `Drain worker ${w.worker_id}? It finishes in-flight work and exits.`,
      { title: "Drain worker", confirmLabel: "Drain", danger: true },
    );
    if (!ok) return;
    this.api.stopWorker(w.worker_id).subscribe({
      next: () => {
        this.ui.toast("success", `Draining ${w.worker_id}.`);
        this.reload(true);
      },
      error: () => this.ui.toast("error", "Could not signal worker."),
    });
  }

  async stopAll(): Promise<void> {
    const n = this.workers().length;
    const ok = await this.ui.confirm(
      `Drain all ${n} worker(s)? Each finishes its shard and exits.`,
      { title: "Drain fleet", confirmLabel: "Drain all", danger: true },
    );
    if (!ok) return;
    this.api.stopAllWorkers().subscribe({
      next: (r) => {
        this.ui.toast("success", `Draining ${r.count} worker(s).`);
        this.reload(true);
      },
      error: () => this.ui.toast("error", "Could not drain the fleet."),
    });
  }

  shortRun(runId?: string): string {
    if (!runId) return "—";
    return runId.length > 10 ? runId.slice(0, 8) + "…" : runId;
  }

  fmt(n?: number): string {
    return (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 1 });
  }

  /** Human-readable RSS (e.g. "212 MB"). Em-dash when a worker hasn't reported
   *  memory yet. The cross-cycle trend + leak alert live in Grafana; this is the
   *  at-a-glance current value. */
  fmtBytes(n?: number): string {
    if (!n || n <= 0) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  }
}
