import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";

import { UiService } from "../shared/ui.service";
import { StatusInfo, SystemInfo, SystemService } from "./system.service";

/**
 * Read-only system posture. Surfaces the deployment's security boundaries,
 * durability and observability — config that otherwise only lives in env vars —
 * plus live service health from /status. Deliberately read-only: the sandbox
 * and auth gate are deploy-time controls, not web-toggleable.
 */
@Component({
  selector: "app-system",
  standalone: true,
  imports: [],
  template: `
    <div class="sys">
      <div class="sys__head">
        <p class="muted">
          How this deployment is configured right now. These are set via
          environment variables at deploy time (see the Configuration doc); this
          panel is read-only.
        </p>
        <button class="btn btn--mini" (click)="reload()" [disabled]="loading()">
          {{ loading() ? "Refreshing…" : "Refresh" }}
        </button>
      </div>

      @if (sys(); as s) {
        <section class="card">
          <h2>Security</h2>
          <div class="rows">
            <div class="row">
              <span class="k">Environment</span>
              <span class="badge" [class.warn]="s.environment === 'unset'">{{
                s.environment
              }}</span>
            </div>
            <div class="row">
              <span class="k">API auth enforced</span>
              <span
                class="badge"
                [class.ok]="s.security.auth_enforced"
                [class.bad]="!s.security.auth_enforced"
              >
                {{ s.security.auth_enforced ? "enforced" : "open (dev)" }}
              </span>
            </div>
            <div class="row">
              <span class="k">Code-node sandbox</span>
              <span
                class="badge"
                [class.ok]="s.security.code_sandbox_mode !== 'off'"
                [class.warn]="s.security.code_sandbox_mode === 'off'"
              >
                {{ s.security.code_sandbox_mode }}
              </span>
            </div>
            <div class="row">
              <span class="k">Network isolation active</span>
              <span
                class="badge"
                [class.ok]="s.security.code_network_isolation_active"
                [class.warn]="!s.security.code_network_isolation_active"
              >
                {{ s.security.code_network_isolation_active ? "yes" : "no" }}
              </span>
            </div>
            <div class="row">
              <span class="k">Unauthenticated code allowed</span>
              <span
                class="badge"
                [class.bad]="s.security.unauth_code_execution_allowed"
                [class.ok]="!s.security.unauth_code_execution_allowed"
              >
                {{
                  s.security.unauth_code_execution_allowed
                    ? "yes (trusted dev only)"
                    : "no"
                }}
              </span>
            </div>
          </div>
        </section>

        <section class="card">
          <h2>Runtime &amp; durability</h2>
          <div class="rows">
            <div class="row">
              <span class="k">Event backbone</span
              ><span class="val">{{ s.runtime.event_backbone }}</span>
            </div>
            <div class="row">
              <span class="k">Redis configured</span>
              <span
                class="badge"
                [class.ok]="s.runtime.redis_configured"
                [class.warn]="!s.runtime.redis_configured"
              >
                {{ s.runtime.redis_configured ? "yes" : "no (single-node)" }}
              </span>
            </div>
            <div class="row">
              <span class="k">Run registry durable</span>
              <span
                class="badge"
                [class.ok]="s.runtime.run_store_durable"
                [class.warn]="!s.runtime.run_store_durable"
              >
                {{
                  s.runtime.run_store_durable ? "on disk" : "in-memory (dev)"
                }}
              </span>
            </div>
            <div class="row">
              <span class="k">Run control</span
              ><span class="val">{{ s.runtime.run_control }}</span>
            </div>
            <div class="row">
              <span class="k">Tracing (OTLP)</span>
              <span
                class="badge"
                [class.ok]="s.runtime.tracing_configured"
                [class.muted-badge]="!s.runtime.tracing_configured"
              >
                {{ s.runtime.tracing_configured ? "configured" : "off" }}
              </span>
            </div>
            <div class="row">
              <span class="k">Version</span
              ><span class="val">{{ s.version }}</span>
            </div>
          </div>
        </section>

        <section class="card">
          <h2>Service health</h2>
          @if (status(); as st) {
            <div class="rows">
              @for (entry of serviceEntries(st); track entry.name) {
                <div class="row">
                  <span class="k">{{ entry.name }}</span>
                  <span
                    class="badge"
                    [class.ok]="entry.status === 'ok'"
                    [class.bad]="entry.status !== 'ok'"
                  >
                    {{ entry.status }}
                  </span>
                </div>
              }
            </div>
          } @else {
            <p class="muted">Checking…</p>
          }
        </section>

        <section class="card">
          <h2>Observability endpoints</h2>
          <p class="muted">
            Operational endpoints exposed by the orchestrator for probes and
            scrapers.
          </p>
          <div class="rows">
            @for (kv of observabilityEntries(s); track kv[0]) {
              <div class="row">
                <span class="k">{{ kv[0] }}</span
                ><code>{{ kv[1] }}</code>
              </div>
            }
          </div>
        </section>
      } @else if (error()) {
        <div class="card">
          <p class="bad">{{ error() }}</p>
        </div>
      } @else {
        <p class="muted">Loading…</p>
      }
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      :host {
        display: block;
      }
      .sys {
        display: flex;
        flex-direction: column;
        gap: 16px;
      }
      .sys__head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
        margin: 0;
      }
      .bad {
        color: var(--color-danger, #f85149);
        font-size: 13px;
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 16px;
        background: var(--color-node, var(--color-bg));
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .card h2 {
        margin: 0;
        font-size: 14px;
      }
      .rows {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        font-size: 13px;
        padding: 4px 0;
        border-bottom: 1px solid
          color-mix(in srgb, var(--color-border) 50%, transparent);
      }
      .row:last-child {
        border-bottom: none;
      }
      .k {
        color: var(--color-text-muted, var(--color-text));
      }
      .val {
        font-family: ui-monospace, Menlo, monospace;
        font-size: 12px;
      }
      code {
        font-family: ui-monospace, Menlo, monospace;
        font-size: 12px;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
        border-radius: 5px;
        padding: 1px 6px;
      }
      .badge {
        font-size: 11px;
        font-weight: 600;
        padding: 2px 9px;
        border-radius: 999px;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
        text-transform: lowercase;
      }
      .badge.ok {
        color: var(--color-success, #2ea043);
        border-color: color-mix(
          in srgb,
          var(--color-success, #2ea043) 50%,
          transparent
        );
      }
      .badge.bad {
        color: var(--color-danger, #f85149);
        border-color: color-mix(
          in srgb,
          var(--color-danger, #f85149) 50%,
          transparent
        );
      }
      .badge.warn {
        color: var(--color-warning, #d29922);
        border-color: color-mix(
          in srgb,
          var(--color-warning, #d29922) 50%,
          transparent
        );
      }
      .badge.muted-badge {
        color: var(--color-text-muted, var(--color-text));
      }
      .btn {
        padding: 7px 14px;
        border-radius: 7px;
        border: 1px solid var(--color-border);
        background: var(--color-bg);
        color: var(--color-text);
        cursor: pointer;
        font: inherit;
        font-size: 13px;
      }
      .btn:hover:not(:disabled) {
        background: var(--color-node);
      }
      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .btn--mini {
        padding: 4px 10px;
        font-size: 12px;
        white-space: nowrap;
      }
    `,
  ],
})
export class SystemComponent implements OnInit {
  private readonly api = inject(SystemService);
  private readonly ui = inject(UiService);

  readonly sys = signal<SystemInfo | null>(null);
  readonly status = signal<StatusInfo | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  ngOnInit() {
    this.reload();
  }

  reload() {
    this.loading.set(true);
    this.error.set(null);
    this.api.system().subscribe({
      next: (s) => {
        this.sys.set(s);
        this.loading.set(false);
      },
      error: (err) => {
        this.loading.set(false);
        this.error.set(this.describeError(err));
      },
    });
    this.api.status().subscribe({
      next: (st) => this.status.set(st),
      error: () => {},
    });
  }

  private describeError(err: { status?: number }): string {
    switch (err?.status) {
      case 0:
        return "Can't reach the orchestrator (network or CORS). Check it's running and that CORS_ORIGINS includes this portal's origin.";
      case 404:
        return "The orchestrator doesn't expose /system — it's running an older build. Rebuild and restart the orchestrator image.";
      case 401:
      case 403:
        return "Not authorized to read system info. Sign in again, or check the orchestrator and auth-service share AUTH_JWT_SECRET.";
      case 503:
        return "The orchestrator is up but not ready (auth not configured, or a dependency is down). See its logs / /ready.";
      default:
        return `Could not load system info (HTTP ${err?.status ?? "?"}).`;
    }
  }

  serviceEntries(st: StatusInfo) {
    return Object.entries(st.services).map(([name, v]) => ({
      name,
      status: v.status,
    }));
  }

  observabilityEntries(s: SystemInfo): [string, string][] {
    return Object.entries(s.observability);
  }
}
