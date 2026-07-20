import { CommonModule } from "@angular/common";
import { Component, OnInit, computed, inject, signal } from "@angular/core";
import { FormsModule } from "@angular/forms";

import { UiService } from "../shared/ui.service";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import {
  NatsClusterHealth,
  NatsConsumer,
  NatsServer,
  NatsStream,
  NatsService,
} from "./nats.service";

type Tab = "health" | "streams" | "servers";

/**
 * NATS cluster console (ADR-0006). One page, three tabs:
 *
 * - **Cluster health** — per-node liveness + JetStream account stats from the
 *   monitoring port (read-only).
 * - **Streams** — list / create / delete JetStream streams and consumers.
 * - **Servers** — the named-cluster registry connections point at.
 *
 * The broker is external infrastructure PayProbe connects to; nothing here
 * manages the broker process itself. NOTE: the Angular portal isn't buildable in
 * the AI sandbox — this component is written to the existing patterns but needs a
 * host `npm run build` to verify.
 */
@Component({
  selector: "app-nats",
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
  ],
  template: `
    <pp-page-header
      subtitle="Broker health + JetStream streams + cluster registry (ADR-0006)"
    />

    <div class="bar">
      <label class="f">
        <span>Cluster</span>
        <select [(ngModel)]="selected" (ngModelChange)="onSelect()">
          <option value="">— pick a registered cluster —</option>
          @for (s of servers(); track s.key) {
            <option [value]="s.key">{{ s.name || s.key }}</option>
          }
        </select>
      </label>
      <label class="f grow">
        <span>…or ad-hoc servers (comma-separated URLs)</span>
        <input
          [(ngModel)]="adhoc"
          (ngModelChange)="selected.set('')"
          placeholder="nats://nats-1:4222, nats://nats-2:4223"
        />
      </label>
      <pp-button variant="secondary" icon="rotate-cw" (click)="refresh()">
        Refresh
      </pp-button>
    </div>

    <p class="hint">
      <pp-icon name="alert-circle" [size]="13" />
      Default cluster is seeded from docker-compose (<code
        >nats://nats-1:4222</code
      >
      …). URLs are resolved by the <strong>orchestrator</strong>: use the
      compose service names when the stack runs in Docker, or
      <button class="link" (click)="useLocalhost()">
        register a localhost cluster
      </button>
      when you run the services on the host (compose publishes client ports
      4222/4223/4224 and monitoring 8222/8223/8224).
    </p>

    <div class="tabs">
      <button
        class="tab"
        [class.tab--on]="tab() === 'health'"
        (click)="tab.set('health')"
      >
        <pp-icon name="activity" [size]="14" /> Cluster health
      </button>
      <button
        class="tab"
        [class.tab--on]="tab() === 'streams'"
        (click)="tab.set('streams'); refreshStreams()"
      >
        <pp-icon name="database" [size]="14" /> Streams
      </button>
      <button
        class="tab"
        [class.tab--on]="tab() === 'servers'"
        (click)="tab.set('servers')"
      >
        <pp-icon name="server" [size]="14" /> Servers
      </button>
    </div>

    @if (error()) {
      <p class="err">
        <pp-icon name="alert-circle" [size]="14" /> {{ error() }}
      </p>
    }

    <!-- ---------------------------------------------------------- HEALTH -->
    @if (tab() === "health") {
      @if (health(); as h) {
        <div class="summary">
          <span
            class="pill"
            [class.pill--ok]="h.healthy"
            [class.pill--warn]="!h.healthy"
          >
            {{ h.live }}/{{ h.total }} nodes healthy
          </span>
          <span class="pill" [class.pill--ok]="h.jetstream">
            JetStream {{ h.jetstream ? "enabled" : "off" }}
          </span>
        </div>
        <table class="grid">
          <thead>
            <tr>
              <th>Node</th>
              <th>Status</th>
              <th>Version</th>
              <th>Conns</th>
              <th>Streams</th>
              <th>Consumers</th>
              <th>Messages</th>
            </tr>
          </thead>
          <tbody>
            @for (n of h.nodes; track n.monitor) {
              <tr>
                <td>{{ n.server_name || n.monitor }}</td>
                <td>
                  <span
                    class="dot"
                    [class.dot--ok]="n.healthy"
                    [class.dot--bad]="!n.healthy"
                  ></span>
                  {{ n.healthy ? "up" : "down" }}
                </td>
                <td>{{ n.version || "—" }}</td>
                <td>{{ n.connections ?? "—" }}</td>
                <td>{{ n.jetstream?.streams ?? "—" }}</td>
                <td>{{ n.jetstream?.consumers ?? "—" }}</td>
                <td>{{ n.jetstream?.messages ?? "—" }}</td>
              </tr>
            }
          </tbody>
        </table>
      } @else {
        <p class="muted">Pick a cluster and Refresh to probe node health.</p>
      }
    }

    <!-- --------------------------------------------------------- STREAMS -->
    @if (tab() === "streams") {
      <div class="newstream">
        <input
          [(ngModel)]="newStreamName"
          placeholder="Stream name (e.g. PAY)"
        />
        <input
          [(ngModel)]="newStreamSubjects"
          placeholder="Subjects (comma-separated, e.g. pay.>)"
        />
        <pp-button variant="primary" icon="plus" (click)="createStream()"
          >Create</pp-button
        >
      </div>
      <table class="grid">
        <thead>
          <tr>
            <th>Stream</th>
            <th>Subjects</th>
            <th>Messages</th>
            <th>Bytes</th>
            <th>Consumers</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          @for (s of streams(); track s.name) {
            <tr>
              <td>{{ s.name }}</td>
              <td>{{ s.subjects.join(", ") }}</td>
              <td>{{ s.messages ?? "—" }}</td>
              <td>{{ s.bytes ?? "—" }}</td>
              <td>
                <button class="link" (click)="loadConsumers(s.name)">
                  {{ s.consumers ?? 0 }} ›
                </button>
              </td>
              <td>
                <button class="danger" (click)="deleteStream(s.name)">
                  Delete
                </button>
              </td>
            </tr>
            @if (openStream() === s.name) {
              <tr class="sub">
                <td colspan="6">
                  <strong>Consumers</strong>
                  @if (consumers().length) {
                    <ul>
                      @for (c of consumers(); track c.durable || c.name) {
                        <li>
                          {{ c.durable || c.name }}
                          <button
                            class="danger sm"
                            (click)="
                              deleteConsumer(s.name, c.durable || c.name!)
                            "
                          >
                            Delete
                          </button>
                        </li>
                      }
                    </ul>
                  } @else {
                    <span class="muted">no consumers</span>
                  }
                </td>
              </tr>
            }
          } @empty {
            <tr>
              <td colspan="6" class="muted">
                No streams (or no cluster selected).
              </td>
            </tr>
          }
        </tbody>
      </table>
    }

    <!-- --------------------------------------------------------- SERVERS -->
    @if (tab() === "servers") {
      <div class="newserver">
        <input
          [(ngModel)]="srvName"
          placeholder="Cluster name (e.g. PayProbe cluster)"
        />
        <input
          [(ngModel)]="srvServers"
          placeholder="Client URLs (comma or newline separated)"
        />
        <input
          [(ngModel)]="srvMonitoring"
          placeholder="Monitoring bases (http://nats-1:8222, …)"
        />
        <pp-button variant="primary" icon="plus" (click)="saveServer()"
          >Save cluster</pp-button
        >
      </div>
      <table class="grid">
        <thead>
          <tr>
            <th>Name</th>
            <th>Servers</th>
            <th>Monitoring</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          @for (s of servers(); track s.key) {
            <tr>
              <td>{{ s.name || s.key }}</td>
              <td>{{ s.servers.join(", ") }}</td>
              <td>{{ (s.monitoring || []).join(", ") || "— (derived)" }}</td>
              <td>
                <button class="danger" (click)="deleteServer(s.key)">
                  Delete
                </button>
              </td>
            </tr>
          } @empty {
            <tr>
              <td colspan="4" class="muted">No registered clusters yet.</td>
            </tr>
          }
        </tbody>
      </table>
    }
  `,
  styles: [
    `
      .bar {
        display: flex;
        gap: 12px;
        align-items: flex-end;
        margin: 12px 0;
        flex-wrap: wrap;
      }
      .f {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .f.grow {
        flex: 1;
        min-width: 240px;
      }
      .f span {
        font-size: 12px;
        color: var(--pp-text-muted);
      }
      .f select,
      .f input,
      .newstream input,
      .newserver input {
        padding: 6px 8px;
        border: 1px solid var(--pp-border);
        border-radius: 6px;
        background: var(--pp-surface);
        color: var(--pp-text);
      }
      .tabs {
        display: flex;
        gap: 4px;
        border-bottom: 1px solid var(--pp-border);
        margin-bottom: 12px;
      }
      .tab {
        display: flex;
        gap: 6px;
        align-items: center;
        padding: 8px 14px;
        background: none;
        border: none;
        color: var(--pp-text-muted);
        cursor: pointer;
        border-bottom: 2px solid transparent;
      }
      .tab--on {
        color: var(--pp-text);
        border-bottom-color: var(--pp-primary);
      }
      .grid {
        width: 100%;
        border-collapse: collapse;
      }
      .grid th,
      .grid td {
        text-align: left;
        padding: 8px 10px;
        border-bottom: 1px solid var(--pp-border);
        font-size: 13px;
      }
      .grid th {
        color: var(--pp-text-muted);
        font-weight: 600;
      }
      .summary {
        display: flex;
        gap: 8px;
        margin-bottom: 12px;
      }
      .pill {
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 12px;
        background: var(--pp-surface-2);
      }
      .pill--ok {
        background: rgba(34, 197, 94, 0.15);
        color: #16a34a;
      }
      .pill--warn {
        background: rgba(234, 179, 8, 0.15);
        color: #ca8a04;
      }
      .dot {
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        margin-right: 4px;
      }
      .dot--ok {
        background: #22c55e;
      }
      .dot--bad {
        background: #ef4444;
      }
      .newstream,
      .newserver {
        display: flex;
        gap: 8px;
        margin-bottom: 12px;
        flex-wrap: wrap;
      }
      .newstream input,
      .newserver input {
        flex: 1;
        min-width: 160px;
      }
      .link {
        background: none;
        border: none;
        color: var(--pp-primary);
        cursor: pointer;
      }
      .danger {
        background: none;
        border: 1px solid var(--pp-border);
        border-radius: 6px;
        color: #ef4444;
        cursor: pointer;
        padding: 4px 10px;
      }
      .danger.sm {
        padding: 2px 8px;
        font-size: 12px;
        margin-left: 8px;
      }
      .sub td {
        background: var(--pp-surface-2);
      }
      .sub ul {
        margin: 6px 0 0;
        padding-left: 18px;
      }
      .muted {
        color: var(--pp-text-muted);
      }
      .err {
        color: #ef4444;
        display: flex;
        gap: 6px;
        align-items: center;
      }
      .hint {
        display: flex;
        gap: 6px;
        align-items: baseline;
        flex-wrap: wrap;
        font-size: 12px;
        color: var(--pp-text-muted);
        margin: 0 0 12px;
      }
      .hint code {
        background: var(--pp-surface-2);
        padding: 1px 5px;
        border-radius: 4px;
      }
      .hint .link {
        font-size: 12px;
      }
    `,
  ],
})
export class NatsComponent implements OnInit {
  private readonly api = inject(NatsService);
  private readonly ui = inject(UiService);

  readonly tab = signal<Tab>("health");
  readonly servers = signal<NatsServer[]>([]);
  readonly health = signal<NatsClusterHealth | null>(null);
  readonly streams = signal<NatsStream[]>([]);
  readonly consumers = signal<NatsConsumer[]>([]);
  readonly openStream = signal<string | null>(null);
  readonly error = signal<string>("");

  selected = signal<string>("");
  adhoc = "";
  newStreamName = "";
  newStreamSubjects = "";
  srvName = "";
  srvServers = "";
  srvMonitoring = "";

  ngOnInit(): void {
    this.reloadServers();
  }

  /** The current target params: registry key wins, else the ad-hoc URL list. */
  private tgt(): { server?: string; servers?: string } {
    if (this.selected()) return { server: this.selected() };
    const s = this.adhoc.trim();
    return s ? { servers: s } : {};
  }

  private hasTarget(): boolean {
    const t = this.tgt();
    return !!(t.server || t.servers);
  }

  reloadServers(): void {
    this.api.listServers().subscribe({
      next: (l) => {
        this.servers.set(l);
        // Auto-select a cluster on first load (the seeded default, or the first)
        // and probe it, so the page shows health without any typing.
        if (!this.selected() && !this.adhoc && l.length) {
          const def = l.find((s) => s.key === "payprobe-cluster") ?? l[0];
          this.selected.set(def.key);
          this.refresh();
        }
      },
      error: () => this.servers.set([]),
    });
  }

  /** Register (and select) a localhost cluster matching the compose port
   *  mappings, for a stack where the services run on the host, not inside the
   *  compose network. Uses explicit monitoring URLs (8222/8223/8224) so the
   *  health tab probes each node — deriving them from the client URLs would
   *  point every node at localhost:8222. */
  useLocalhost(): void {
    this.api
      .saveServer("localhost cluster", {
        name: "localhost cluster",
        description: "Host-published compose ports",
        servers: [
          "nats://localhost:4222",
          "nats://localhost:4223",
          "nats://localhost:4224",
        ],
        monitoring: [
          "http://localhost:8222",
          "http://localhost:8223",
          "http://localhost:8224",
        ],
      })
      .subscribe({
        next: (s) => {
          this.adhoc = "";
          this.reloadServers();
          this.selected.set(s.key);
          this.refresh();
        },
        error: (e) => this.ui.toast("error", this.msg(e)),
      });
  }

  onSelect(): void {
    this.adhoc = "";
    this.refresh();
  }

  refresh(): void {
    this.error.set("");
    if (this.tab() === "servers") return this.reloadServers();
    if (!this.hasTarget()) return;
    this.refreshHealth();
    if (this.tab() === "streams") this.refreshStreams();
  }

  refreshHealth(): void {
    const t = this.tgt();
    this.api.cluster(t.server, t.servers).subscribe({
      next: (h) => this.health.set(h),
      error: (e) => this.error.set(this.msg(e)),
    });
  }

  refreshStreams(): void {
    if (!this.hasTarget()) return;
    const t = this.tgt();
    this.api.streams(t.server, t.servers).subscribe({
      next: (r) => this.streams.set(r.streams),
      error: (e) => this.error.set(this.msg(e)),
    });
  }

  createStream(): void {
    const subjects = this.split(this.newStreamSubjects);
    if (!this.newStreamName.trim() || !subjects.length) {
      this.ui.toast("error", "A stream needs a name and at least one subject.");
      return;
    }
    this.api
      .createStream({
        name: this.newStreamName.trim(),
        subjects,
        ...this.tgt(),
      })
      .subscribe({
        next: () => {
          this.ui.toast("success", `Stream ${this.newStreamName} created.`);
          this.newStreamName = "";
          this.newStreamSubjects = "";
          this.refreshStreams();
        },
        error: (e) => this.ui.toast("error", this.msg(e)),
      });
  }

  deleteStream(name: string): void {
    const t = this.tgt();
    this.api.deleteStream(name, t.server, t.servers).subscribe({
      next: () => {
        this.ui.toast("success", `Stream ${name} deleted.`);
        this.refreshStreams();
      },
      error: (e) => this.ui.toast("error", this.msg(e)),
    });
  }

  loadConsumers(stream: string): void {
    if (this.openStream() === stream) {
      this.openStream.set(null);
      return;
    }
    const t = this.tgt();
    this.api.consumers(stream, t.server, t.servers).subscribe({
      next: (r) => {
        this.consumers.set(r.consumers);
        this.openStream.set(stream);
      },
      error: (e) => this.ui.toast("error", this.msg(e)),
    });
  }

  deleteConsumer(stream: string, durable: string): void {
    const t = this.tgt();
    this.api.deleteConsumer(stream, durable, t.server, t.servers).subscribe({
      next: () => {
        this.ui.toast("success", `Consumer ${durable} deleted.`);
        this.openStream.set(null);
        this.loadConsumers(stream);
      },
      error: (e) => this.ui.toast("error", this.msg(e)),
    });
  }

  saveServer(): void {
    if (!this.srvName.trim() || !this.split(this.srvServers).length) {
      this.ui.toast(
        "error",
        "A cluster needs a name and at least one server URL.",
      );
      return;
    }
    this.api
      .saveServer(this.srvName.trim(), {
        name: this.srvName.trim(),
        servers: this.split(this.srvServers),
        monitoring: this.split(this.srvMonitoring),
      })
      .subscribe({
        next: () => {
          this.ui.toast("success", "Cluster saved.");
          this.srvName = "";
          this.srvServers = "";
          this.srvMonitoring = "";
          this.reloadServers();
        },
        error: (e) => this.ui.toast("error", this.msg(e)),
      });
  }

  deleteServer(key: string): void {
    this.api.deleteServer(key).subscribe({
      next: () => {
        this.ui.toast("success", "Cluster removed.");
        this.reloadServers();
      },
      error: (e) => this.ui.toast("error", this.msg(e)),
    });
  }

  private split(s: string): string[] {
    return (s || "")
      .split(/[\n,]+/)
      .map((x) => x.trim())
      .filter(Boolean);
  }

  private msg(e: unknown): string {
    const err = e as { error?: { detail?: string }; message?: string };
    return err?.error?.detail || err?.message || "request failed";
  }
}
