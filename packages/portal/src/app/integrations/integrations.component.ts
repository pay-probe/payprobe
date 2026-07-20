import {
  Component,
  OnInit,
  inject,
  signal,
  computed,
  ChangeDetectionStrategy,
} from "@angular/core";

import { ScenarioApiService } from "../constructor/scenario-api.service";
import { ThemeService } from "../shared/theme.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { MCP_CATALOG, McpCatalogGroup } from "./mcp-catalog.generated";

@Component({
  selector: "pp-integrations",
  standalone: true,
  imports: [PageHeaderComponent],
  template: `
    <div class="intg">
      <pp-page-header>
        <div subtitle>
          <span class="muted">
            Expose the step catalog and scenario operations to AI agents over
            the Model Context Protocol
          </span>
        </div>
      </pp-page-header>

      <div class="intg__body">
        <!-- status -->
        <section class="card">
          <div class="card__head">
            <h2>Status</h2>
            <button
              class="btn btn--sm"
              (click)="check()"
              [disabled]="checking()"
            >
              {{ checking() ? "Checking…" : "Re-check" }}
            </button>
          </div>
          <div class="status">
            <span
              class="dot"
              [class.ok]="reachable() === true"
              [class.bad]="reachable() === false"
            ></span>
            @if (reachable() === true) {
              <span
                >Backing services reachable — {{ stepCount() }} step types in
                the catalog. All {{ catalog.toolCount }} MCP tools are ready to
                serve.</span
              >
            } @else if (reachable() === false) {
              <span
                >Scenario service not reachable. The MCP server proxies these
                APIs, so start the stack before connecting an agent.</span
              >
            } @else {
              <span class="muted">Checking backing services…</span>
            }
          </div>
          <p class="muted small">
            The MCP server is a thin proxy over scenario-service (catalog,
            scenarios, registries) and the orchestrator (runs, load). If those
            are up, every tool below works. The browser can't start or stop the
            server process itself — it's launched by your MCP client (stdio) or
            runs as a container (Streamable HTTP).
          </p>
        </section>

        <!-- auth -->
        <section class="card card--warn">
          <div class="card__head">
            <h2>Authentication required</h2>
          </div>
          <p class="muted small">
            scenario-service and the orchestrator run a fail-closed JWT gate, so
            the MCP server must present a credential on every upstream call. Set
            <code class="mono">AUTH_JWT_SECRET</code> (the same shared secret
            the services verify with) and the server mints a short-lived service
            JWT automatically. Alternatively, pass a ready-made bearer token via
            <code class="mono">MCP_API_TOKEN</code>. Without one of these, every
            tool returns 401 even when the services are up.
          </p>
        </section>

        <!-- tools -->
        <section class="card">
          <div class="card__head">
            <h2>
              Tools <span class="muted">({{ catalog.toolCount }})</span>
            </h2>
            <input
              class="filter"
              type="search"
              placeholder="Filter tools…"
              [value]="query()"
              (input)="onFilter($event)"
            />
          </div>
          <p class="muted small">
            Generated from the server's tool registry — what you see here is
            exactly what the agent can call. Badges show behaviour:
            <span class="badge read">read</span> is safe to retry,
            <span class="badge create">create</span>/<span class="badge upsert"
              >upsert</span
            >
            write, <span class="badge run">run</span> reaches a live endpoint,
            and <span class="badge delete">delete</span>/<span
              class="badge stop"
              >stop</span
            >
            tear something down (clients should confirm first).
          </p>
          @for (g of visibleGroups(); track g.name) {
            <h3 class="grp">
              {{ g.name }} <span class="muted">({{ g.tools.length }})</span>
            </h3>
            <table class="tools">
              <tbody>
                @for (t of g.tools; track t.name) {
                  <tr>
                    <td class="mono name">{{ t.name }}</td>
                    <td class="muted">{{ t.title }}</td>
                    <td class="kind">
                      <span class="badge" [class]="'badge ' + t.kind">{{
                        t.kind
                      }}</span>
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          }
          @if (visibleGroups().length === 0) {
            <p class="muted small">No tools match "{{ query() }}".</p>
          }
        </section>

        <!-- resources -->
        <section class="card">
          <div class="card__head">
            <h2>
              Resources
              <span class="muted">({{ catalog.resources.length }})</span>
            </h2>
          </div>
          <p class="muted small">
            Read-only views the agent can subscribe to by URI — handy for
            grounding without a tool call. Templated URIs take one id.
          </p>
          <table class="tools">
            <tbody>
              @for (r of catalog.resources; track r.uri) {
                <tr>
                  <td class="mono name res">{{ r.uri }}</td>
                  <td class="muted">{{ r.description }}</td>
                </tr>
              }
            </tbody>
          </table>
        </section>

        <!-- prompts -->
        <section class="card">
          <div class="card__head">
            <h2>
              Prompts <span class="muted">({{ catalog.prompts.length }})</span>
            </h2>
          </div>
          <p class="muted small">
            Reusable workflows a client can surface as slash-commands. Each
            steers the agent through the right tools in the right order.
          </p>
          <table class="tools">
            <tbody>
              @for (p of catalog.prompts; track p.name) {
                <tr>
                  <td class="mono name">{{ p.name }}</td>
                  <td>
                    <div>{{ p.title }}</div>
                    <div class="muted small">{{ p.description }}</div>
                  </td>
                </tr>
              }
            </tbody>
          </table>
        </section>

        <!-- HTTP (docker) -->
        <section class="card">
          <div class="card__head">
            <h2>Connect over HTTP (Docker)</h2>
          </div>
          <p class="muted small">
            Brought up by
            <code class="mono">docker compose up mcp-server</code>. It runs
            Streamable HTTP transport and connects to the other services on the
            internal network. Point any HTTP-capable MCP client at:
          </p>
          <div class="snip">
            <code class="mono">{{ mcpUrl }}</code>
            <button class="btn btn--sm" (click)="copy(mcpUrl)">Copy</button>
          </div>
          <p class="muted small">Configured environment (compose):</p>
          <div class="snip snip--block">
            <pre class="mono">{{ httpEnv }}</pre>
            <button class="btn btn--sm" (click)="copy(httpEnv)">Copy</button>
          </div>
        </section>

        <!-- stdio (Claude Desktop) -->
        <section class="card">
          <div class="card__head">
            <h2>Connect over stdio (Claude Desktop)</h2>
          </div>
          <p class="muted small">
            Add to <code class="mono">claude_desktop_config.json</code>. The
            client launches the server as a subprocess; set the URLs to wherever
            the services are reachable from your machine, and
            <code class="mono">AUTH_JWT_SECRET</code> to the shared secret the
            services verify with.
          </p>
          <div class="snip snip--block">
            <pre class="mono">{{ stdioConfig }}</pre>
            <button class="btn btn--sm" (click)="copy(stdioConfig)">
              Copy
            </button>
          </div>
        </section>
      </div>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .intg {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .intg__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 16px;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
      }
      .small {
        font-size: 12px;
      }
      .mono {
        font-family: var(--font-mono, ui-monospace, monospace);
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 14px 16px;
      }
      .card--warn {
        border-color: var(--color-warning, #d29922);
        background: color-mix(
          in srgb,
          var(--color-warning, #d29922) 8%,
          transparent
        );
      }
      .card__head {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .card__head h2 {
        margin: 0;
        font-size: 14px;
        flex: 1 1 auto;
      }
      .filter {
        flex: 0 0 auto;
        width: 180px;
        padding: 5px 9px;
        border-radius: 7px;
        border: 1px solid var(--color-border);
        background: var(--color-bg);
        color: var(--color-text);
        font: inherit;
        font-size: 12px;
      }
      .grp {
        margin: 14px 0 4px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--color-text-muted, #888);
      }
      .status {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 10px 0;
        font-size: 13px;
      }
      .dot {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: var(--color-text-muted, #888);
        flex: 0 0 auto;
      }
      .dot.ok {
        background: var(--color-success, #2ea043);
      }
      .dot.bad {
        background: var(--color-danger, #f85149);
      }
      .tools {
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
      }
      .tools td {
        padding: 5px 8px;
        border-bottom: 1px solid var(--color-border);
        vertical-align: top;
      }
      .tools .name {
        white-space: nowrap;
        color: var(--color-primary, #58a6ff);
      }
      .tools .name.res {
        white-space: normal;
        word-break: break-all;
      }
      .kind {
        text-align: right;
        white-space: nowrap;
        width: 1%;
      }
      .badge {
        display: inline-block;
        font-size: 10px;
        line-height: 1.6;
        padding: 0 7px;
        border-radius: 999px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        border: 1px solid var(--color-border);
        color: var(--color-text-muted, #888);
      }
      .badge.read {
        color: var(--color-primary, #58a6ff);
        border-color: color-mix(
          in srgb,
          var(--color-primary, #58a6ff) 40%,
          transparent
        );
      }
      .badge.create,
      .badge.upsert {
        color: var(--color-success, #2ea043);
        border-color: color-mix(
          in srgb,
          var(--color-success, #2ea043) 40%,
          transparent
        );
      }
      .badge.run {
        color: var(--color-warning, #d29922);
        border-color: color-mix(
          in srgb,
          var(--color-warning, #d29922) 40%,
          transparent
        );
      }
      .badge.delete,
      .badge.stop {
        color: var(--color-danger, #f85149);
        border-color: color-mix(
          in srgb,
          var(--color-danger, #f85149) 40%,
          transparent
        );
      }
      .snip {
        display: flex;
        align-items: center;
        gap: 10px;
        margin: 8px 0;
        background: var(--color-node, var(--color-bg));
        border: 1px solid var(--color-border);
        border-radius: 8px;
        padding: 8px 10px;
      }
      .snip code {
        flex: 1 1 auto;
        word-break: break-all;
        font-size: 12px;
      }
      .snip--block {
        align-items: flex-start;
      }
      .snip pre {
        flex: 1 1 auto;
        margin: 0;
        font-size: 12px;
        white-space: pre-wrap;
        word-break: break-word;
      }
      .btn {
        padding: 6px 12px;
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
        opacity: 0.5;
        cursor: not-allowed;
      }
      .btn--sm {
        padding: 4px 10px;
        font-size: 12px;
        flex: 0 0 auto;
      }
      code.mono {
        font-size: 0.92em;
      }
    `,
  ],
})
export class IntegrationsComponent implements OnInit {
  private readonly api = inject(ScenarioApiService);
  readonly theme = inject(ThemeService);

  /** The full MCP surface, generated from the server's registry. */
  readonly catalog = MCP_CATALOG;

  readonly reachable = signal<boolean | null>(null);
  readonly stepCount = signal(0);
  readonly checking = signal(false);
  readonly query = signal("");

  /** Tool groups filtered by the search box (group or tool name/title match). */
  readonly visibleGroups = computed<McpCatalogGroup[]>(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return this.catalog.groups;
    const out: McpCatalogGroup[] = [];
    for (const g of this.catalog.groups) {
      if (g.name.toLowerCase().includes(q)) {
        out.push(g);
        continue;
      }
      const tools = g.tools.filter(
        (t) =>
          t.name.toLowerCase().includes(q) || t.title.toLowerCase().includes(q),
      );
      if (tools.length) out.push({ ...g, tools });
    }
    return out;
  });

  readonly mcpUrl = "http://localhost:8200/mcp";
  readonly httpEnv = [
    "mcp-server:",
    "  environment:",
    "    SCENARIO_API_URL: http://scenario-service:8000",
    "    RUN_API_URL: http://orchestrator:8100",
    "    AUTH_JWT_SECRET: ${AUTH_JWT_SECRET}   # same shared secret the services verify with",
    "    MCP_TRANSPORT: streamable-http",
    "    MCP_PORT: 8200",
  ].join("\n");
  readonly stdioConfig = JSON.stringify(
    {
      mcpServers: {
        payprobe: {
          command: "python",
          args: ["-m", "mcp_server"],
          env: {
            SCENARIO_API_URL: "http://localhost:8000",
            RUN_API_URL: "http://localhost:8100",
            AUTH_JWT_SECRET: "<shared-secret>",
          },
        },
      },
    },
    null,
    2,
  );

  ngOnInit(): void {
    this.check();
  }

  onFilter(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
  }

  check(): void {
    this.checking.set(true);
    this.api.catalog().subscribe({
      next: (targets) => {
        this.stepCount.set(targets?.length ?? 0);
        this.reachable.set(true);
        this.checking.set(false);
      },
      error: () => {
        this.reachable.set(false);
        this.checking.set(false);
      },
    });
  }

  copy(text: string): void {
    void navigator.clipboard?.writeText(text);
  }
}
