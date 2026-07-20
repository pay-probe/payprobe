import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import { AuthService } from "../auth/auth.service";
import { ScenarioApiService } from "../constructor/scenario-api.service";
import { AssistConfig } from "../constructor/scenario.models";
import { RuntimeConfigService } from "../shared/runtime-config.service";
import { IconComponent } from "../shared/ui/icon.component";
import { UiService } from "../shared/ui.service";
import { EndpointsComponent } from "./endpoints.component";
import { SystemComponent } from "./system.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

type SettingsTab = "assistant" | "auth" | "endpoints" | "system";

/**
 * Settings — AI assistant LLM provider config. The API key is write-only: the
 * page shows whether one is set (+ a masked hint) but never receives it back.
 * Leave the key field blank to keep the stored key when changing other fields.
 */
@Component({
  selector: "app-settings",
  standalone: true,
  imports: [
    PageHeaderComponent,
    FormsModule,
    IconComponent,
    EndpointsComponent,
    SystemComponent,
  ],
  template: `
    <div class="st">
      <pp-page-header>
        <div subtitle>
          <span class="muted">{{ tabSubtitle() }}</span>
        </div>
      </pp-page-header>

      <div class="st__layout">
        <nav class="subnav">
          <button
            class="subnav__item"
            [class.active]="tab() === 'assistant'"
            (click)="tab.set('assistant')"
          >
            <pp-icon name="zap" [size]="16" /> AI assistant
          </button>
          <button
            class="subnav__item"
            [class.active]="tab() === 'auth'"
            (click)="tab.set('auth')"
          >
            <pp-icon name="shield" [size]="16" /> Authentication
          </button>
          <button
            class="subnav__item"
            [class.active]="tab() === 'endpoints'"
            (click)="tab.set('endpoints')"
          >
            <pp-icon name="activity" [size]="16" /> Endpoints
          </button>
          <button
            class="subnav__item"
            [class.active]="tab() === 'system'"
            (click)="tab.set('system')"
          >
            <pp-icon name="shield" [size]="16" /> System
          </button>
        </nav>

        <div class="st__body">
          @if (tab() === "assistant") {
            <section class="card">
              <h2>AI assistant — LLM provider</h2>
              <p class="muted">
                The “✨ Ask AI” assistant uses a built-in heuristic by default.
                Add an API key here to switch it to a real model (any
                OpenAI-compatible endpoint — OpenAI, Azure OpenAI, a local
                Ollama/LM Studio, …).
              </p>

              @if (cfg(); as c) {
                <label class="fld">
                  <span>Provider</span>
                  <select
                    [(ngModel)]="provider"
                    (ngModelChange)="onProviderChange($event)"
                  >
                    <option value="openai">OpenAI (GPT)</option>
                    <option value="anthropic">Anthropic (Claude)</option>
                  </select>
                </label>

                <label class="fld fld--row">
                  <input type="checkbox" [(ngModel)]="enabled" />
                  <span
                    >Use the LLM provider
                    {{ c.key_set ? "" : "(set a key first)" }}</span
                  >
                </label>

                <label class="fld">
                  <span>Endpoint URL</span>
                  <input
                    type="text"
                    [(ngModel)]="baseUrl"
                    [placeholder]="
                      provider === 'anthropic'
                        ? 'https://api.anthropic.com/v1/messages'
                        : 'https://api.openai.com/v1/chat/completions'
                    "
                  />
                </label>

                <label class="fld">
                  <span>Model</span>
                  <input
                    type="text"
                    [(ngModel)]="model"
                    list="model-opts"
                    [placeholder]="
                      provider === 'anthropic'
                        ? 'claude-3-5-sonnet-latest'
                        : 'gpt-4o-mini'
                    "
                  />
                  <datalist id="model-opts">
                    @for (m of modelSuggestions; track m) {
                      <option [value]="m"></option>
                    }
                  </datalist>
                  <small class="muted"
                    >Use the exact model id from your provider's console (free
                    text).</small
                  >
                </label>

                <label class="fld">
                  <span>
                    API key
                    @if (c.key_set) {
                      <em class="muted"
                        >— stored {{ c.key_hint }} (leave blank to keep)</em
                      >
                    }
                  </span>
                  <input
                    type="password"
                    [(ngModel)]="apiKey"
                    placeholder="sk-…"
                    autocomplete="off"
                  />
                </label>

                <div class="actions">
                  <button
                    class="btn btn--primary"
                    (click)="save()"
                    [disabled]="saving()"
                  >
                    {{ saving() ? "Saving…" : "Save" }}
                  </button>
                  <button
                    class="btn"
                    (click)="test()"
                    [disabled]="testing() || !c.key_set"
                  >
                    {{ testing() ? "Testing…" : "Test connection" }}
                  </button>
                  @if (c.key_set) {
                    <button class="btn btn--danger" (click)="clearKey()">
                      Remove key
                    </button>
                  }
                  <span class="actions__sp"></span>
                  <span class="status" [class.on]="c.enabled && c.key_set">
                    {{
                      c.enabled && c.key_set
                        ? "● LLM active"
                        : "● Heuristic (no LLM)"
                    }}
                  </span>
                </div>

                @if (testResult(); as t) {
                  <div class="result" [class.ok]="t.ok" [class.bad]="!t.ok">
                    @if (t.ok) {
                      ✓ Connected to {{ t.model }} — model replied “{{
                        t.reply
                      }}”.
                      @if (!t.enabled) {
                        <span class="muted"> (toggle on + Save to use it)</span>
                      }
                    } @else {
                      ✗ {{ t.error }}
                    }
                  </div>
                }

                <p class="note muted">
                  Save your key first, then Test connection. The key is stored
                  server-side and never shown again; for production prefer an
                  environment variable / secret manager.
                </p>
              } @else {
                <p class="muted">Loading…</p>
              }
            </section>
          }

          @if (tab() === "auth") {
            <!-- ───────────────────────── Authentication ───────────────────────── -->
            <section class="card">
              <h2>Authentication — your session</h2>
              <p class="muted">
                The portal signs in against the <strong>auth-service</strong>,
                which issues a JWT that the orchestrator and scenario-service
                verify with a shared secret. Tokens carry your roles and project
                scope.
              </p>

              @if (auth.user(); as u) {
                <div class="kv">
                  <div class="kv__row">
                    <span class="kv__k">Signed in as</span
                    ><span class="kv__v">{{ u.username }}</span>
                  </div>
                  <div class="kv__row">
                    <span class="kv__k">Roles</span>
                    <span class="kv__v">
                      @for (r of u.roles; track r) {
                        <span class="chip">{{ r }}</span>
                      }
                      @if (!u.roles.length) {
                        <em class="muted">none</em>
                      }
                    </span>
                  </div>
                  <div class="kv__row">
                    <span class="kv__k">Project scope</span>
                    <span class="kv__v">
                      @for (p of u.project_ids; track p) {
                        <span class="chip">{{
                          p === "*" ? "all projects" : p
                        }}</span>
                      }
                      @if (!u.project_ids.length) {
                        <em class="muted">none</em>
                      }
                    </span>
                  </div>
                  <div class="kv__row">
                    <span class="kv__k">Auth endpoint</span
                    ><span class="kv__v"
                      ><code>{{ authBase }}</code></span
                    >
                  </div>
                  <div class="kv__row">
                    <span class="kv__k">Token</span
                    ><span class="kv__v"
                      ><span class="status on">● active</span> (stored in this
                      browser)</span
                    >
                  </div>
                </div>
                <div class="actions">
                  <button class="btn btn--danger" (click)="auth.logout()">
                    Sign out
                  </button>
                </div>
              } @else {
                <p class="muted">Not signed in.</p>
              }
            </section>

            <section class="card">
              <h2>auth-service — configuration</h2>
              <p class="muted">
                Set these as environment variables on the
                <code>auth-service</code> container (see
                <code>infra/docker/docker-compose.yml</code>). Defaults are
                dev-only — set real values before deploying.
              </p>
              <div class="tbl-wrap">
                <table class="tbl">
                  <thead>
                    <tr>
                      <th>Variable</th>
                      <th>Default</th>
                      <th>Purpose</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (p of authParams; track p.name) {
                      <tr>
                        <td>
                          <code>{{ p.name }}</code>
                        </td>
                        <td class="nowrap muted">{{ p.def }}</td>
                        <td>{{ p.desc }}</td>
                      </tr>
                    }
                  </tbody>
                </table>
              </div>

              <h2 style="margin-top:6px">Verifier services</h2>
              <p class="muted">
                The orchestrator and scenario-service verify tokens locally. The
                gate
                <strong>fails closed</strong>: outside <code>dev</code>/<code
                  >test</code
                >
                it returns 503 until a secret or token is configured.
              </p>
              <div class="tbl-wrap">
                <table class="tbl">
                  <thead>
                    <tr>
                      <th>Variable</th>
                      <th>Applies to</th>
                      <th>Purpose</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (p of verifierParams; track p.name) {
                      <tr>
                        <td>
                          <code>{{ p.name }}</code>
                        </td>
                        <td class="muted">{{ p.svc }}</td>
                        <td>{{ p.desc }}</td>
                      </tr>
                    }
                  </tbody>
                </table>
              </div>
            </section>

            <section class="card">
              <h2>Endpoints &amp; usage</h2>
              <div class="tbl-wrap">
                <table class="tbl">
                  <thead>
                    <tr>
                      <th>Method</th>
                      <th>Path</th>
                      <th>Auth</th>
                      <th>Purpose</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (e of endpoints; track e.p + e.m) {
                      <tr>
                        <td>
                          <span class="meth" [attr.data-m]="e.m">{{
                            e.m
                          }}</span>
                        </td>
                        <td>
                          <code>{{ e.p }}</code>
                        </td>
                        <td class="muted">{{ e.a }}</td>
                        <td>{{ e.d }}</td>
                      </tr>
                    }
                  </tbody>
                </table>
              </div>

              <div class="snip">
                <div class="snip__head">
                  <span>Get a token</span>
                  <button class="btn btn--mini" (click)="copy(tokenCmd)">
                    Copy
                  </button>
                </div>
                <pre><code>{{ tokenCmd }}</code></pre>
              </div>

              <div class="snip">
                <div class="snip__head">
                  <span>Call a gated endpoint</span>
                  <button class="btn btn--mini" (click)="copy(callCmd)">
                    Copy
                  </button>
                </div>
                <pre><code>{{ callCmd }}</code></pre>
              </div>

              <p class="note muted">
                Default dev credentials are <code>admin</code> /
                <code>admin</code>. Change <code>AUTH_ADMIN_PASSWORD</code> and
                set a strong <code>AUTH_JWT_SECRET</code> before exposing the
                service.
              </p>
            </section>
          }

          @if (tab() === "endpoints") {
            <app-endpoints />
          }

          @if (tab() === "system") {
            <app-system />
          }
        </div>
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
      .st {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .st__bar {
        padding: 12px 16px;
        border-bottom: 1px solid var(--color-border);
      }
      .st__bar h1 {
        margin: 0;
        font-size: 16px;
      }
      .st__layout {
        flex: 1 1 auto;
        display: flex;
        min-height: 0;
      }
      .subnav {
        flex: 0 0 210px;
        border-right: 1px solid var(--color-border);
        padding: 14px 10px;
        display: flex;
        flex-direction: column;
        gap: 2px;
        overflow: auto;
      }
      .subnav__item {
        display: flex;
        align-items: center;
        gap: 10px;
        width: 100%;
        padding: 9px 12px;
        border: none;
        border-radius: 8px;
        background: none;
        color: var(--color-text);
        font: inherit;
        font-size: 13px;
        text-align: left;
        cursor: pointer;
      }
      .subnav__item:hover {
        background: var(--color-node, var(--color-bg));
      }
      .subnav__item.active {
        background: var(--color-node, var(--color-bg));
        color: var(--color-primary, #58a6ff);
        font-weight: 600;
      }
      .st__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: 18px;
        display: flex;
        flex-direction: column;
        gap: 16px;
      }
      @media (max-width: 680px) {
        .st__layout {
          flex-direction: column;
        }
        .subnav {
          flex-basis: auto;
          flex-direction: row;
          flex-wrap: wrap;
          border-right: none;
          border-bottom: 1px solid var(--color-border);
        }
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 16px;
        background: var(--color-node, var(--color-bg));
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .card h2 {
        margin: 0;
        font-size: 15px;
      }
      .fld {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 12px;
      }
      .fld span {
        color: var(--color-text-muted, var(--color-text));
      }
      .fld input[type="text"],
      .fld input[type="password"],
      .fld select {
        padding: 8px 10px;
        border: 1px solid var(--color-border);
        border-radius: 7px;
        background: var(--color-bg);
        color: var(--color-text);
        font: inherit;
        font-size: 13px;
      }
      .fld--row {
        flex-direction: row;
        align-items: center;
        gap: 8px;
      }
      .fld--row span {
        color: var(--color-text);
        font-size: 13px;
      }
      .actions {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .actions__sp {
        flex: 1 1 auto;
      }
      .status {
        font-size: 12px;
        color: var(--color-text-muted, var(--color-text));
      }
      .status.on {
        color: var(--color-success, #2ea043);
      }
      .note {
        margin: 0;
        font-size: 11px;
      }
      .result {
        font-size: 12px;
        padding: 8px 10px;
        border-radius: 7px;
        border: 1px solid var(--color-border);
      }
      .result.ok {
        color: var(--color-success, #2ea043);
        border-color: var(--color-success, #2ea043);
      }
      .result.bad {
        color: var(--color-danger, #f85149);
        border-color: var(--color-danger, #f85149);
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
      .btn:hover {
        background: var(--color-node);
      }
      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .btn--primary {
        background: var(--color-primary, #58a6ff);
        border-color: var(--color-primary, #58a6ff);
        color: #fff;
      }
      .btn--danger {
        color: var(--color-danger, #f85149);
        border-color: var(--color-danger, #f85149);
        background: transparent;
      }
      .btn--mini {
        padding: 3px 9px;
        font-size: 11px;
      }
      code {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 12px;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
        border-radius: 5px;
        padding: 1px 5px;
      }
      .kv {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .kv__row {
        display: flex;
        gap: 12px;
        align-items: baseline;
        font-size: 13px;
      }
      .kv__k {
        flex: 0 0 120px;
        color: var(--color-text-muted, var(--color-text));
        font-size: 12px;
      }
      .kv__v {
        flex: 1 1 auto;
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        align-items: center;
      }
      .chip {
        font-size: 11px;
        padding: 2px 8px;
        border-radius: 999px;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
      }
      .tbl-wrap {
        overflow-x: auto;
        border: 1px solid var(--color-border);
        border-radius: 8px;
      }
      .tbl {
        width: 100%;
        border-collapse: collapse;
        font-size: 12px;
      }
      .tbl th,
      .tbl td {
        text-align: left;
        padding: 7px 10px;
        border-bottom: 1px solid var(--color-border);
        vertical-align: top;
      }
      .tbl thead th {
        color: var(--color-text-muted, var(--color-text));
        font-weight: 600;
        background: var(--color-node, var(--color-bg));
      }
      .tbl tr:last-child td {
        border-bottom: none;
      }
      .tbl code {
        background: none;
        border: none;
        padding: 0;
      }
      .nowrap {
        white-space: nowrap;
      }
      .meth {
        font-family: ui-monospace, monospace;
        font-size: 10px;
        font-weight: 700;
        padding: 2px 6px;
        border-radius: 5px;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
      }
      .meth[data-m="POST"] {
        color: var(--color-success, #2ea043);
      }
      .meth[data-m="DELETE"] {
        color: var(--color-danger, #f85149);
      }
      .meth[data-m="GET"] {
        color: var(--color-primary, #58a6ff);
      }
      .snip {
        border: 1px solid var(--color-border);
        border-radius: 8px;
        overflow: hidden;
      }
      .snip__head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 6px 10px;
        background: var(--color-node, var(--color-bg));
        font-size: 12px;
        color: var(--color-text-muted, var(--color-text));
      }
      .snip pre {
        margin: 0;
        padding: 10px;
        overflow-x: auto;
        font-size: 12px;
        line-height: 1.5;
        background: var(--color-bg);
      }
      .snip pre code {
        background: none;
        border: none;
        padding: 0;
      }
    `,
  ],
})
export class SettingsComponent implements OnInit {
  private readonly api = inject(ScenarioApiService);
  private readonly ui = inject(UiService);
  private readonly runtimeCfg = inject(RuntimeConfigService);
  readonly auth = inject(AuthService);

  readonly tab = signal<SettingsTab>("assistant");
  readonly tabSubtitle = computed(
    () =>
      ({
        assistant: "Configure the ✨ Ask AI assistant",
        auth: "Sessions, parameters & usage",
        endpoints: "Service hosts, ports & reachability",
        system: "Runtime configuration & service health",
      })[this.tab()],
  );

  // -- auth reference (static docs surfaced in the UI) -----------------------
  readonly authBase = this.runtimeCfg.authApiBase;

  readonly authParams = [
    {
      name: "AUTH_JWT_SECRET",
      def: "(required in prod)",
      desc: "HS256 secret used to sign tokens. Verifier services must share the exact same value.",
    },
    {
      name: "AUTH_DB",
      def: "/data/auth.db",
      desc: "SQLite path for the user store (persisted on the auth_data volume).",
    },
    {
      name: "AUTH_TOKEN_TTL_SEC",
      def: "3600",
      desc: "Access-token lifetime, in seconds.",
    },
    {
      name: "AUTH_JWT_ISSUER",
      def: "payprobe-auth",
      desc: "iss claim written into tokens and checked by verifiers.",
    },
    {
      name: "AUTH_ADMIN_USER",
      def: "admin",
      desc: "Username of the admin account seeded on first start.",
    },
    {
      name: "AUTH_ADMIN_PASSWORD",
      def: "(dev: admin)",
      desc: "Bootstrap admin password. In prod, leaving it unset seeds no user.",
    },
    {
      name: "CORS_ORIGINS",
      def: "localhost:8080,4200",
      desc: "Comma-separated browser origins allowed to call the service.",
    },
    {
      name: "PAYPROBE_ENV",
      def: "dev",
      desc: "dev/test relaxes fail-closed behaviour; any other value requires auth to be configured.",
    },
  ];

  readonly verifierParams = [
    {
      name: "AUTH_JWT_SECRET",
      svc: "orchestrator, scenario-service",
      desc: "Same secret as auth-service — lets each service verify JWTs locally, no callback.",
    },
    {
      name: "API_TOKEN",
      svc: "orchestrator, scenario-service",
      desc: "Optional static bearer accepted alongside JWTs (service-to-service).",
    },
    {
      name: "PAYPROBE_ENV",
      svc: "all services",
      desc: "Outside dev/test the gate fails closed (503) until a secret or token is set.",
    },
  ];

  readonly endpoints = [
    {
      m: "POST",
      p: "/token",
      a: "none",
      d: "Password grant → { access_token }",
    },
    {
      m: "GET",
      p: "/me",
      a: "bearer",
      d: "Roles + project scope for the current token",
    },
    {
      m: "GET",
      p: "/verify",
      a: "bearer",
      d: "Token introspection → { active, claims }",
    },
    { m: "GET", p: "/users", a: "admin", d: "List users" },
    {
      m: "POST",
      p: "/users",
      a: "admin",
      d: "Create a user (roles, project_ids)",
    },
    { m: "DELETE", p: "/users/{name}", a: "admin", d: "Delete a user" },
    { m: "GET", p: "/health", a: "none", d: "Liveness" },
  ];

  readonly tokenCmd =
    `TOKEN=$(curl -s -X POST ${this.authBase}/token \\\n` +
    `  -d username=admin -d password=admin | jq -r .access_token)`;
  readonly callCmd = `curl ${this.runtimeCfg.runApiBase}/runs -H "Authorization: Bearer $TOKEN"`;

  copy(text: string) {
    navigator.clipboard?.writeText(text).then(
      () => this.ui.toast("success", "Copied to clipboard."),
      () => this.ui.toast("error", "Copy failed."),
    );
  }

  readonly cfg = signal<AssistConfig | null>(null);
  readonly saving = signal(false);
  readonly testing = signal(false);
  readonly testResult = signal<{
    ok: boolean;
    enabled: boolean;
    model?: string;
    reply?: string;
    error?: string;
  } | null>(null);

  provider: "openai" | "anthropic" = "openai";
  enabled = false;
  baseUrl = "";
  model = "";
  apiKey = "";

  private readonly providerDefaults = {
    openai: {
      url: "https://api.openai.com/v1/chat/completions",
      model: "gpt-4o-mini",
    },
    anthropic: {
      url: "https://api.anthropic.com/v1/messages",
      model: "claude-3-5-sonnet-latest",
    },
  };

  /** Example model ids for the picker (free text — exact id varies by account). */
  get modelSuggestions(): string[] {
    return this.provider === "anthropic"
      ? [
          "claude-opus-4-8",
          "claude-sonnet-4-6",
          "claude-haiku-4-5-20251001",
          "claude-3-5-sonnet-latest",
          "claude-3-5-haiku-latest",
          "claude-3-opus-latest",
        ]
      : ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4-turbo", "gpt-3.5-turbo"];
  }

  ngOnInit() {
    this.reload();
  }

  reload() {
    this.api.getAssistConfig().subscribe({
      next: (c) => {
        this.cfg.set(c);
        this.provider = c.provider;
        this.enabled = c.enabled;
        this.baseUrl = c.base_url;
        this.model = c.model;
        this.apiKey = "";
      },
      error: () => this.ui.toast("error", "Could not load settings."),
    });
  }

  /** Switch provider -> prefill its default endpoint + model. */
  onProviderChange(p: "openai" | "anthropic") {
    const d = this.providerDefaults[p];
    this.baseUrl = d.url;
    this.model = d.model;
    this.testResult.set(null);
  }

  save() {
    this.saving.set(true);
    this.api
      .saveAssistConfig({
        provider: this.provider,
        enabled: this.enabled,
        base_url: this.baseUrl.trim(),
        model: this.model.trim(),
        // only send the key when the user typed one (blank = keep existing)
        api_key: this.apiKey ? this.apiKey : null,
      })
      .subscribe({
        next: (c) => {
          this.saving.set(false);
          this.cfg.set(c);
          this.apiKey = "";
          this.ui.toast("success", "Settings saved.");
        },
        error: () => {
          this.saving.set(false);
          this.ui.toast("error", "Save failed.");
        },
      });
  }

  test() {
    this.testing.set(true);
    this.testResult.set(null);
    this.api.testAssistConfig().subscribe({
      next: (t) => {
        this.testing.set(false);
        this.testResult.set(t);
      },
      error: () => {
        this.testing.set(false);
        this.testResult.set({
          ok: false,
          enabled: false,
          error: "request failed (is the service running?)",
        });
      },
    });
  }

  clearKey() {
    this.api
      .saveAssistConfig({
        provider: this.provider,
        enabled: false,
        base_url: this.baseUrl.trim(),
        model: this.model.trim(),
        api_key: "",
      })
      .subscribe({
        next: (c) => {
          this.cfg.set(c);
          this.enabled = false;
          this.ui.toast("success", "Key removed.");
        },
        error: () => this.ui.toast("error", "Failed to remove key."),
      });
  }
}
