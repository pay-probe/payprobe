import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { Router } from "@angular/router";

import { ScenarioApiService } from "../constructor/scenario-api.service";
import {
  ActionSpec,
  MessageFormat,
  Project,
  TargetSpec,
} from "../constructor/scenario.models";
import { UiService } from "../shared/ui.service";
import { BadgeComponent } from "../shared/ui/badge.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import {
  PlaygroundApiService,
  PlaygroundHistoryRow,
  PlaygroundResult,
  PlaygroundSample,
  PlaygroundTargetRef,
  PlaygroundTargets,
} from "./playground-api.service";

/** A row in the target rail (one per addressable thing, whatever its kind). */
interface TargetRow {
  kind: PlaygroundTargetRef["kind"];
  id: string;
  label: string;
  detail: string;
  environments?: string[];
  disabled?: boolean;
  // wire shape → which catalog family's actions apply (composer action picker).
  adapter?: string;
  protocol?: string;
  // samples.wire family, resolved SERVER-side (raw targets resolve client-side).
  sampleFamily?: string | null;
}

/** Per-target-kind accent — icon + rail label, so a rich network of things you
 *  can poke reads at a glance. Colors echo the DS section accents. */
const KIND_COLOR: Record<string, string> = {
  connection: "#ec4899", // pink — Configure/Connections
  simulator: "#06b6d4", // cyan — Simulated Network
  participant: "#8b5cf6", // violet — flows
  group: "#f59e0b", // amber — fleets
  function: "#a855f7", // purple — crypto
  insight: "#f0883e", // orange — Intelligence (the catalog's insight accent)
  raw: "#64748b", // slate — hand-typed
};
const KIND_ICON: Record<string, string> = {
  connection: "plug",
  simulator: "server",
  participant: "flow",
  group: "group",
  function: "key",
  insight: "sparkles",
  raw: "terminal",
};

/** Non-tcp adapter → wire family aliases (mirrors the server's `_ADAPTER_ALIAS`
 *  in playground_samples.py). Only consulted for raw hand-typed targets; every
 *  registry row already carries the server-resolved `sample_family`. */
const SAMPLE_FAMILY_ALIAS: Record<string, string> = {
  tcp_iso8583: "iso8583",
  switch: "iso8583",
  acquirer: "iso8583",
  hsm: "header_echo",
  hsm_tcp: "header_echo",
  hsm_client: "payshield",
  rest_pay: "http",
};

const ISO_SAMPLE = JSON.stringify(
  { mti: "0200", values: { "2": "4000001234567890", "4": "000000001000" } },
  null,
  2,
);

/**
 * Playground (ADR-0007) — one place to act on ANY addressable resource
 * interactively: fire a message through a registered connection (resolved
 * server-side, secrets never round-trip), at a running simulator or network
 * participant, through a group, at a raw host/port, run a crypto function, or
 * ask the advisory insight service (ADR-0005) for a model opinion.
 * Echoed payloads arrive masked; explorations promote to scenarios.
 */
@Component({
  selector: "app-playground",
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
    <div class="pg">
      <pp-page-header
        subtitle="Poke anything that is addressable right now — no scenario authoring needed"
      >
        <pp-button
          variant="secondary"
          size="sm"
          icon="activity"
          (click)="reload()"
        >
          Refresh targets
        </pp-button>
      </pp-page-header>

      <div class="pg__grid">
        <!-- ============ target rail ============ -->
        <aside class="rail">
          <div class="rail__search">
            <pp-icon name="search" [size]="14" />
            <input
              [ngModel]="filter()"
              (ngModelChange)="filter.set($event)"
              placeholder="Filter targets…"
            />
          </div>

          @for (group of groups(); track group.title) {
            @if (group.rows.length) {
              <section class="rail__group" [style.--k]="group.color">
                <h4>
                  <span class="dot" [style.background]="group.color"></span>
                  {{ group.title }}
                  <span class="rail__count">{{ group.rows.length }}</span>
                </h4>
                @for (row of group.rows; track row.kind + row.id) {
                  <button
                    class="trow"
                    [style.--k]="kindColor(row.kind)"
                    [class.trow--active]="isSelected(row)"
                    [class.trow--off]="row.disabled"
                    (click)="select(row)"
                  >
                    <pp-icon
                      class="trow__icon"
                      [name]="iconFor(row.kind)"
                      [size]="14"
                    />
                    <span class="trow__label">{{ row.label }}</span>
                    <span class="trow__detail">{{ row.detail }}</span>
                  </button>
                }
              </section>
            }
          }

          <section class="rail__group" [style.--k]="kindColor('raw')">
            <h4>
              <span class="dot" [style.background]="kindColor('raw')"></span>
              Raw endpoint
            </h4>
            <button
              class="trow"
              [style.--k]="kindColor('raw')"
              [class.trow--active]="selected()?.kind === 'raw'"
              (click)="selectRaw()"
            >
              <pp-icon class="trow__icon" name="terminal" [size]="14" />
              <span class="trow__label">host : port</span>
              <span class="trow__detail">hand-typed</span>
            </button>
          </section>
        </aside>

        <!-- ============ composer + response ============ -->
        <main class="main">
          @if (!selected()) {
            <div class="empty">
              <span class="empty__icon"
                ><pp-icon name="beaker" [size]="28"
              /></span>
              <p>
                Pick a target on the left — a connection, a running simulator or
                participant, a group, a crypto function, the insight model, or
                a raw endpoint.
              </p>
            </div>
          } @else {
            <section class="composer" [style.--k]="kindColor(selected()!.kind)">
              <header class="composer__head">
                <span
                  class="composer__badge"
                  [style.background]="kindColor(selected()!.kind)"
                >
                  <pp-icon [name]="iconFor(selected()!.kind)" [size]="15" />
                </span>
                <strong>{{ selected()!.label }}</strong>
                <span class="kindtag" [style.--k]="kindColor(selected()!.kind)">
                  {{ selected()!.kind }}
                </span>
                @if (selected()!.detail) {
                  <span class="muted">{{ selected()!.detail }}</span>
                }
              </header>

              @if (selected()!.kind === "raw") {
                <div class="frow">
                  <label
                    >Host <input [(ngModel)]="rawHost" placeholder="127.0.0.1"
                  /></label>
                  <label
                    >Port
                    <input
                      [(ngModel)]="rawPort"
                      type="number"
                      placeholder="7001"
                  /></label>
                  <label
                    >Protocol
                    <select
                      [(ngModel)]="rawProtocol"
                      (ngModelChange)="onRawProtocolChange()"
                    >
                      <option value="iso8583">iso8583</option>
                      <option value="header_echo">
                        header_echo (HSM link)
                      </option>
                      <option value="payshield">payShield host commands</option>
                    </select>
                  </label>
                </div>
              }

              @if (connectionEnvs().length) {
                <div class="frow">
                  <label
                    >Environment (override matrix)
                    <select
                      [ngModel]="environment()"
                      (ngModelChange)="environment.set($event)"
                    >
                      <option value="">— base config —</option>
                      @for (e of connectionEnvs(); track e) {
                        <option [value]="e">{{ e }}</option>
                      }
                    </select>
                  </label>
                </div>
              }

              <div class="frow">
                @if (selected()!.kind === "function") {
                  <label
                    >Function
                    <select
                      [ngModel]="action()"
                      (ngModelChange)="action.set($event)"
                    >
                      @for (op of cryptoOps(); track op) {
                        <option [value]="op">{{ op }}</option>
                      }
                    </select>
                  </label>
                } @else {
                  <label
                    >Action
                    @if (catalogActions().length) {
                      <select
                        [ngModel]="actionChoice()"
                        (ngModelChange)="pickAction($event)"
                      >
                        @for (a of catalogActions(); track a.name) {
                          <option [value]="a.name">
                            {{ a.label }} ({{ a.name }})
                          </option>
                        }
                        <option value="__custom__">Custom action…</option>
                      </select>
                    } @else {
                      <input
                        [ngModel]="action()"
                        (ngModelChange)="action.set($event)"
                        placeholder="send_message"
                      />
                    }
                  </label>
                  @if (customAction()) {
                    <label
                      >Custom action name
                      <input
                        [ngModel]="action()"
                        (ngModelChange)="action.set($event)"
                        placeholder="send_message"
                      />
                    </label>
                  }
                  @if (isoLike()) {
                    <label
                      >Message format (dialect)
                      <select
                        [ngModel]="formatId()"
                        (ngModelChange)="formatId.set($event)"
                      >
                        <option value="">connection default</option>
                        @for (f of formats(); track f.id) {
                          <option [value]="f.id">{{ f.name || f.id }}</option>
                        }
                      </select>
                    </label>
                  }
                }
              </div>

              @if (samplesFor().length) {
                <div class="samples">
                  <span class="samples__label">
                    <pp-icon name="beaker" [size]="13" /> Samples
                  </span>
                  @for (s of samplesFor(); track s.name) {
                    <button
                      class="chip"
                      [class.chip--active]="s.name === appliedSample()"
                      [title]="s.notes ?? ''"
                      (click)="applySample(s)"
                    >
                      {{ s.name }}
                    </button>
                  }
                </div>
                @if (sampleNotes(); as notes) {
                  <p class="hint">
                    <pp-icon name="info" [size]="13" />
                    <span>{{ notes }}</span>
                  </p>
                }
              }

              @if (actionHint(); as hint) {
                <p class="hint">
                  <pp-icon name="info" [size]="13" />
                  <span>
                    {{ hint.summary }}
                    @if (hint.fields) {
                      <span class="muted"> — payload: {{ hint.fields }}</span>
                    }
                  </span>
                  @if (hint.canPrefill) {
                    <button class="linkbtn" (click)="prefillPayload()">
                      insert skeleton
                    </button>
                  }
                </p>
              }

              <label class="payload">
                Payload (JSON)
                <textarea
                  [ngModel]="payloadText()"
                  (ngModelChange)="onPayloadEdit($event)"
                  rows="8"
                  spellcheck="false"
                ></textarea>
              </label>
              @if (payloadError()) {
                <p class="jsonerr">{{ payloadError() }}</p>
              }

              <div class="composer__actions">
                <pp-button
                  icon="play"
                  [disabled]="busy() || !!payloadError()"
                  (click)="execute()"
                >
                  {{ busy() ? "Firing…" : "Execute" }}
                </pp-button>
                <span class="muted">
                  Sends real traffic to the resolved endpoint. Echoes come back
                  masked.
                </span>
              </div>
            </section>

            @if (result(); as r) {
              <section class="result" [class.result--fail]="!r.ok">
                <header class="result__head">
                  <pp-badge [tone]="r.ok ? 'success' : 'danger'">
                    {{ r.status }}
                  </pp-badge>
                  <span class="muted">{{ r.duration_ms }} ms</span>
                  <code class="muted">{{ describeTarget(r.target) }}</code>
                </header>
                @if (r.error) {
                  <p class="result__error">{{ r.error }}</p>
                }
                <div class="result__panes">
                  <div>
                    <h5>Request (masked)</h5>
                    <pre>{{ pretty(r.request) }}</pre>
                  </div>
                  <div>
                    <h5>Response (masked)</h5>
                    <pre>{{ pretty(r.response) }}</pre>
                  </div>
                </div>
              </section>
            }
          }
        </main>

        <!-- ============ history rail ============ -->
        <aside class="hist">
          <header class="hist__head">
            <h4>History</h4>
            <div class="hist__actions">
              <pp-button
                variant="secondary"
                size="sm"
                icon="package"
                [disabled]="!history().length"
                (click)="openSave()"
              >
                Save as scenario
              </pp-button>
              <pp-button
                variant="ghost"
                size="sm"
                icon="x"
                [disabled]="!history().length"
                (click)="clearHistory()"
              >
                Clear
              </pp-button>
            </div>
          </header>

          @if (picked().size) {
            <p class="muted hist__pickinfo">
              {{ picked().size }} of {{ history().length }} selected — only
              these will promote.
              <button class="linkbtn" (click)="clearPicks()">
                clear selection
              </button>
            </p>
          }

          @if (saving()) {
            <div class="savebox">
              <label
                >Scenario name
                <input
                  [(ngModel)]="saveName"
                  placeholder="playground exploration"
                />
              </label>
              <label
                >Project (optional)
                <select [(ngModel)]="saveProject">
                  <option value="">— no project —</option>
                  @for (p of projects(); track p.id) {
                    <option [value]="p.id">{{ p.name }}</option>
                  }
                </select>
              </label>
              <p class="muted">
                Promotes {{ promoteCount() }} entr{{
                  promoteCount() === 1 ? "y" : "ies"
                }}
                ({{ picked().size ? "selected" : "all" }}). Values are masked —
                a template, replace card data before running for real.
              </p>
              <div class="savebox__actions">
                <pp-button size="sm" icon="check" (click)="confirmSave()">
                  Create scenario
                </pp-button>
                <pp-button variant="ghost" size="sm" (click)="cancelSave()">
                  Cancel
                </pp-button>
              </div>
            </div>
          }

          @if (!history().length) {
            <p class="muted hist__empty">
              Nothing yet — every execute lands here. Re-fire any entry, or
              promote the exploration into a durable scenario.
            </p>
          }
          @for (row of history(); track row.seq) {
            <div
              class="hrow"
              [style.--k]="kindColor($any(row.target)?.kind)"
              [class.hrow--fail]="!row.ok"
              [class.hrow--pick]="isPicked(row.seq)"
            >
              <div class="hrow__line">
                <input
                  type="checkbox"
                  class="hrow__check"
                  [checked]="isPicked(row.seq)"
                  (change)="togglePick(row.seq)"
                  title="Select to promote just this entry"
                />
                <span class="hrow__seq">#{{ row.seq }}</span>
                <pp-icon
                  class="hrow__icon"
                  [name]="iconFor($any(row.target)?.kind)"
                  [size]="12"
                />
                <span class="hrow__what" [title]="row.action">
                  {{ row.label || row.action }}
                  <span class="muted">→ {{ describeTarget(row.target) }}</span>
                </span>
                <pp-badge [tone]="row.ok ? 'success' : 'danger'">
                  {{ row.status }}
                </pp-badge>
              </div>
              <div class="hrow__line hrow__meta">
                <span class="muted">{{ row.duration_ms }} ms</span>
                <button class="linkbtn" (click)="refire(row.seq)">
                  re-fire
                </button>
                <button class="linkbtn" (click)="inspect(row)">view</button>
              </div>
            </div>
          }
        </aside>
      </div>
    </div>
  `,
  styles: [
    `
      .pg {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .pg__grid {
        display: grid;
        grid-template-columns: 280px minmax(0, 1fr) 320px;
        gap: 12px;
        align-items: start;
      }
      @media (max-width: 1100px) {
        .pg__grid {
          grid-template-columns: 1fr;
        }
      }
      .muted {
        color: var(--text-muted);
        font-size: 12px;
      }

      /* -- target rail -- */
      .rail {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg, 12px);
        padding: 10px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        max-height: calc(100vh - 180px);
        overflow: auto;
      }
      .rail__search {
        display: flex;
        align-items: center;
        gap: 6px;
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 6px 8px;
        color: var(--text-muted);
      }
      .rail__search input {
        border: 0;
        outline: 0;
        background: transparent;
        color: var(--text);
        width: 100%;
        font: inherit;
        font-size: 12.5px;
      }
      .rail__group h4 {
        display: flex;
        align-items: center;
        gap: 6px;
        margin: 6px 2px 6px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--text-muted);
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex: none;
        box-shadow: 0 0 0 3px
          color-mix(in srgb, var(--k, var(--brand)) 22%, transparent);
      }
      .rail__count {
        margin-left: auto;
        font-size: 10px;
        font-weight: 700;
        color: var(--k, var(--text-muted));
        background: color-mix(in srgb, var(--k, var(--brand)) 14%, transparent);
        border-radius: 999px;
        padding: 1px 7px;
      }
      .trow {
        display: flex;
        align-items: center;
        gap: 8px;
        width: 100%;
        text-align: left;
        background: transparent;
        border: 1px solid transparent;
        border-left: 2px solid transparent;
        border-radius: 8px;
        padding: 6px 8px;
        cursor: pointer;
        color: var(--text);
        font: inherit;
        font-size: 12.5px;
        transition:
          background 0.12s ease,
          border-color 0.12s ease;
      }
      .trow__icon {
        color: var(--k, var(--text-muted));
        flex: none;
      }
      .trow:hover {
        background: color-mix(in srgb, var(--k, #808080) 8%, transparent);
        border-left-color: color-mix(
          in srgb,
          var(--k, #808080) 45%,
          transparent
        );
      }
      .trow--active {
        border-color: var(--k, var(--brand));
        border-left-color: var(--k, var(--brand));
        background: color-mix(in srgb, var(--k, var(--brand)) 13%, transparent);
      }
      .trow--off {
        opacity: 0.45;
      }
      .trow__label {
        font-weight: 600;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .trow__detail {
        margin-left: auto;
        color: var(--text-muted);
        font-size: 11px;
        white-space: nowrap;
      }

      /* -- composer -- */
      .main {
        display: flex;
        flex-direction: column;
        gap: 12px;
        min-width: 0;
      }
      .empty {
        border: 1px dashed var(--border);
        border-radius: var(--radius-lg, 12px);
        padding: 40px 24px;
        text-align: center;
        color: var(--text-muted);
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 12px;
      }
      .empty__icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 56px;
        height: 56px;
        border-radius: 16px;
        color: #22c55e;
        background: color-mix(in srgb, #22c55e 14%, transparent);
      }
      .composer {
        position: relative;
        background: var(--surface);
        border: 1px solid var(--border);
        border-top: 3px solid var(--k, var(--brand));
        border-radius: var(--radius-lg, 12px);
        padding: 14px;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .composer__head {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .composer__badge {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 26px;
        height: 26px;
        border-radius: 8px;
        color: #fff;
        flex: none;
      }
      .kindtag {
        font-size: 10.5px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--k, var(--brand));
        background: color-mix(in srgb, var(--k, var(--brand)) 15%, transparent);
        border: 1px solid
          color-mix(in srgb, var(--k, var(--brand)) 35%, transparent);
        border-radius: 999px;
        padding: 1px 8px;
      }
      .frow {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
      }
      .frow label,
      .payload {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 12px;
        color: var(--text-muted);
        flex: 1;
        min-width: 140px;
      }
      .frow input,
      .frow select,
      .payload textarea {
        border: 1px solid var(--border);
        border-radius: 8px;
        background: var(--surface);
        color: var(--text);
        padding: 7px 9px;
        font-size: 13px;
        font-family: inherit;
      }
      .payload textarea {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 12.5px;
        resize: vertical;
      }
      .jsonerr {
        color: var(--danger, #ef4444);
        font-size: 12px;
        margin: 0;
      }
      .hint {
        display: flex;
        align-items: center;
        gap: 6px;
        margin: 0;
        font-size: 12px;
        color: var(--text-muted);
      }
      .hint .linkbtn {
        margin-left: auto;
      }
      .composer__actions {
        display: flex;
        align-items: center;
        gap: 10px;
      }

      /* -- sample chips -- */
      .samples {
        display: flex;
        align-items: center;
        gap: 6px;
        flex-wrap: wrap;
      }
      .samples__label {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--text-muted);
      }
      .chip {
        border: 1px solid
          color-mix(in srgb, var(--k, var(--brand)) 35%, transparent);
        border-radius: 999px;
        background: transparent;
        color: var(--text);
        padding: 3px 10px;
        font: inherit;
        font-size: 12px;
        cursor: pointer;
        transition:
          background 0.12s ease,
          border-color 0.12s ease;
      }
      .chip:hover {
        background: color-mix(in srgb, var(--k, var(--brand)) 10%, transparent);
      }
      .chip--active {
        border-color: var(--k, var(--brand));
        background: color-mix(in srgb, var(--k, var(--brand)) 15%, transparent);
        font-weight: 600;
      }

      /* -- result -- */
      .result {
        background: var(--surface);
        border: 1px solid var(--border);
        border-left: 3px solid var(--success, #10b981);
        border-radius: var(--radius-lg, 12px);
        padding: 12px 14px;
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .result--fail {
        border-left-color: var(--danger, #ef4444);
      }
      .result__head {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .result__error {
        color: var(--danger, #ef4444);
        font-size: 12.5px;
        margin: 0;
      }
      .result__panes {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px;
      }
      @media (max-width: 900px) {
        .result__panes {
          grid-template-columns: 1fr;
        }
      }
      .result__panes h5 {
        margin: 0 0 4px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--text-muted);
      }
      .result__panes pre {
        margin: 0;
        background: var(--surface-2, rgba(128, 128, 128, 0.06));
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 8px 10px;
        font-size: 12px;
        max-height: 320px;
        overflow: auto;
        white-space: pre-wrap;
        word-break: break-word;
      }

      /* -- history rail -- */
      .hist {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg, 12px);
        padding: 10px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        max-height: calc(100vh - 180px);
        overflow: auto;
      }
      .hist__head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        flex-wrap: wrap;
      }
      .hist__head h4 {
        margin: 0;
        font-size: 13px;
      }
      .hist__actions {
        display: flex;
        gap: 6px;
      }
      .hist__empty {
        padding: 8px 2px;
      }
      .hist__pickinfo {
        margin: 0;
        padding: 2px;
      }
      .savebox {
        border: 1px solid var(--brand);
        border-radius: 10px;
        padding: 10px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: color-mix(in srgb, var(--brand) 6%, transparent);
      }
      .savebox label {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 12px;
        color: var(--text-muted);
      }
      .savebox input,
      .savebox select {
        border: 1px solid var(--border);
        border-radius: 8px;
        background: var(--surface);
        color: var(--text);
        padding: 6px 8px;
        font-size: 13px;
      }
      .savebox__actions {
        display: flex;
        gap: 6px;
      }
      .hrow__check {
        margin: 0;
        cursor: pointer;
        accent-color: var(--brand);
      }
      .hrow--pick {
        border-color: var(--brand);
        background: color-mix(in srgb, var(--brand) 8%, transparent);
      }
      .hrow {
        border: 1px solid var(--border);
        border-left: 3px solid var(--k, var(--success, #10b981));
        border-radius: 8px;
        padding: 7px 9px;
        display: flex;
        flex-direction: column;
        gap: 4px;
        transition: background 0.12s ease;
      }
      .hrow:hover {
        background: color-mix(in srgb, var(--k, #808080) 6%, transparent);
      }
      .hrow--fail {
        border-left-color: var(--danger, #ef4444);
      }
      .hrow__line {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 12.5px;
        min-width: 0;
      }
      .hrow__icon {
        color: var(--k, var(--text-muted));
        flex: none;
      }
      .hrow__seq {
        color: var(--text-muted);
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 11px;
      }
      .hrow__what {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        min-width: 0;
      }
      .hrow__meta {
        justify-content: flex-end;
      }
      .linkbtn {
        background: none;
        border: 0;
        padding: 0;
        color: var(--brand);
        cursor: pointer;
        font-size: 12px;
      }
      .linkbtn:hover {
        text-decoration: underline;
      }
    `,
  ],
})
export class PlaygroundComponent implements OnInit {
  private readonly api = inject(PlaygroundApiService);
  private readonly scenarioApi = inject(ScenarioApiService);
  private readonly ui = inject(UiService);
  private readonly router = inject(Router);

  readonly targets = signal<PlaygroundTargets | null>(null);
  readonly formats = signal<MessageFormat[]>([]);
  readonly catalog = signal<TargetSpec[]>([]);
  readonly projects = signal<Project[]>([]);
  readonly history = signal<PlaygroundHistoryRow[]>([]);
  readonly filter = signal("");

  //: history seqs the operator has ticked to promote. Empty = promote all.
  readonly picked = signal<Set<number>>(new Set());
  //: the save dialog is open; holds its draft name + chosen project.
  readonly saving = signal(false);
  saveName = "playground exploration";
  saveProject = "";

  readonly selected = signal<TargetRow | null>(null);
  readonly environment = signal<string>("");
  readonly action = signal("send_message");
  readonly customAction = signal(false);
  readonly formatId = signal<string>("");
  readonly payloadText = signal(ISO_SAMPLE);
  readonly busy = signal(false);
  readonly result = signal<PlaygroundResult | null>(null);

  // raw endpoint form (plain fields — ngModel two-way on signals is overkill here)
  rawHost = "127.0.0.1";
  rawPort: number | null = null;
  rawProtocol = "iso8583";

  readonly cryptoOps = computed(() => this.targets()?.functions?.crypto ?? []);

  readonly connectionEnvs = computed(() => {
    const sel = this.selected();
    return sel?.kind === "connection" ? (sel.environments ?? []) : [];
  });

  /** ISO-8583-family target → the dialect (Message Format) picker applies. */
  readonly isoLike = computed(() => this.familyKey() === "tcp_iso8583");

  /** Which catalog TargetSpec key a selected target's wire shape maps to.
   *  Mirrors the orchestrator's `_target_type_key` families so the composer
   *  offers the SAME actions an authored step would have. `null` when we can't
   *  tell (raw with no protocol, or a function) — the picker falls back to
   *  free text. */
  readonly familyKey = computed<string | null>(() => {
    const sel = this.selected();
    if (!sel || sel.kind === "function") return null;
    // the insight target maps 1:1 onto the catalog's `insight` TargetSpec —
    // the composer offers the SAME actions the scenario predict step has.
    if (sel.kind === "insight") return "insight";
    const adapter = (
      sel.kind === "raw" ? this.rawProtocol : (sel.adapter ?? "")
    ).toLowerCase();
    const proto = (
      sel.kind === "raw" ? this.rawProtocol : (sel.protocol ?? "")
    ).toLowerCase();
    if (adapter === "nats" || proto === "nats") return "nats";
    if (adapter === "http" || proto === "http" || proto === "cybersource")
      return "http";
    if (
      ["payshield", "hsm_client", "hsm", "hsm_tcp"].includes(adapter) ||
      ["payshield", "header_echo"].includes(proto)
    )
      return "hsm";
    if (adapter.startsWith("db_probe")) return "db_probe_core";
    // tcp / switch / acquirer / iso8583 / visa → the universal ISO 8583 link.
    return "tcp_iso8583";
  });

  /** Which samples.wire family the selected target draws samples from.
   *  Registry-backed rows carry the SERVER-resolved `sample_family`; the
   *  client mapping (protocol → adapter, with shared-shape aliases) only
   *  remains for raw hand-typed targets, which have no registry row. */
  readonly sampleFamily = computed<string | null>(() => {
    const sel = this.selected();
    if (!sel || sel.kind === "function") return null;
    const wire = this.targets()?.samples?.wire ?? {};
    if (sel.sampleFamily && wire[sel.sampleFamily]?.length) {
      return sel.sampleFamily;
    }
    const proto = (sel.protocol ?? "").toLowerCase();
    const adapter = (sel.adapter ?? "").toLowerCase();
    // Mirror the server resolver: protocol wins when meaningful (tcp / no
    // adapter → any known protocol; other adapters → only a deliberately
    // non-default protocol); otherwise the adapter names the family.
    const tcpLike = adapter === "" || adapter === "tcp";
    const order = [
      ...(tcpLike || proto !== "iso8583"
        ? [proto, SAMPLE_FAMILY_ALIAS[proto]]
        : []),
      adapter,
      SAMPLE_FAMILY_ALIAS[adapter],
      adapter === "tcp" ? "iso8583" : "",
    ];
    for (const key of order) {
      if (key && wire[key]?.length) return key;
    }
    return null;
  });

  /** Ready-made samples for the selected target: its wire family's set, or —
   *  for a function target — the one runnable parameter set per operation. */
  readonly samplesFor = computed<PlaygroundSample[]>(() => {
    const sel = this.selected();
    const samples = this.targets()?.samples;
    if (!sel || !samples) return [];
    if (sel.kind === "function") {
      return (samples.functions?.crypto ?? []).filter(
        (s) => s.action === sel.id,
      );
    }
    const fam = this.sampleFamily();
    return fam ? (samples.wire[fam] ?? []) : [];
  });

  /** Name of the last applied sample (chip highlight); cleared on edits. */
  readonly appliedSample = signal<string | null>(null);

  /** Notes of the applied sample, shown under the chips. */
  readonly sampleNotes = computed(() => {
    const name = this.appliedSample();
    if (!name) return null;
    return this.samplesFor().find((s) => s.name === name)?.notes ?? null;
  });

  /** The catalog actions offered for the selected target's family. */
  readonly catalogActions = computed<ActionSpec[]>(() => {
    const key = this.familyKey();
    if (!key) return [];
    return this.catalog().find((t) => t.target === key)?.actions ?? [];
  });

  /** The <select> value: a real action name, or the "custom" sentinel. */
  readonly actionChoice = computed(() =>
    this.customAction() ? "__custom__" : this.action(),
  );

  /** One-line hint for the chosen action, from the catalog. */
  readonly actionHint = computed<{
    summary: string;
    fields: string;
    canPrefill: boolean;
  } | null>(() => {
    const spec = this.catalogActions().find((a) => a.name === this.action());
    if (!spec) return null;
    const hint = spec.payload_hint ?? {};
    const fields = Object.entries(hint)
      .map(([k, v]) => `${k}: ${v}`)
      .join(", ");
    return {
      summary: spec.label,
      fields,
      canPrefill: Object.keys(hint).length > 0,
    };
  });

  readonly payloadError = computed(() => {
    try {
      const v = JSON.parse(this.payloadText() || "{}");
      return typeof v === "object" && v !== null && !Array.isArray(v)
        ? ""
        : "payload must be a JSON object";
    } catch (e) {
      return `invalid JSON: ${(e as Error).message}`;
    }
  });

  /** The rail, grouped and filtered. Each group carries its kind's accent
   *  color + icon so the rail reads as a colorful catalog of pokeable things. */
  readonly groups = computed(() => {
    const t = this.targets();
    const q = this.filter().trim().toLowerCase();
    const hit = (s: string) => !q || s.toLowerCase().includes(q);
    const rows = (list: TargetRow[]) =>
      list.filter((r) => hit(r.label) || hit(r.detail));
    if (!t)
      return [] as {
        title: string;
        color: string;
        icon: string;
        rows: TargetRow[];
      }[];
    return [
      {
        title: "Connections",
        color: KIND_COLOR["connection"],
        icon: KIND_ICON["connection"],
        rows: rows(
          t.connections.map((c) => ({
            kind: "connection" as const,
            id: c.name,
            label: c.name,
            detail: c.environments.length
              ? `envs: ${c.environments.join(", ")}`
              : (c.adapter ?? ""),
            environments: c.environments,
            disabled: c.disabled,
            adapter: c.adapter,
            protocol: c.protocol,
            sampleFamily: c.sample_family,
          })),
        ),
      },
      {
        title: "Running simulators",
        color: KIND_COLOR["simulator"],
        icon: KIND_ICON["simulator"],
        rows: rows(
          t.simulators.map((s) => ({
            kind: "simulator" as const,
            id: s.id,
            label: s.label,
            detail: s.endpoint ?? s.protocol,
            protocol: s.protocol,
            sampleFamily: s.sample_family,
          })),
        ),
      },
      {
        title: "Network participants",
        color: KIND_COLOR["participant"],
        icon: KIND_ICON["participant"],
        rows: rows(
          t.participants.map((p) => ({
            kind: "participant" as const,
            id: p.id,
            label: p.flow_name || p.flow_id,
            detail: p.endpoint ?? p.owner,
            protocol: p.protocol,
            sampleFamily: p.sample_family,
          })),
        ),
      },
      {
        title: "Participant groups",
        color: KIND_COLOR["group"],
        icon: KIND_ICON["group"],
        rows: rows(
          t.groups.map((g) => ({
            kind: "group" as const,
            id: g.name || g.id || "",
            label: g.name || g.id || "?",
            detail: `${g.members} member${g.members === 1 ? "" : "s"}`,
            adapter: g.adapter,
            sampleFamily: g.sample_family,
          })),
        ),
      },
      {
        title: "Functions (crypto)",
        color: KIND_COLOR["function"],
        icon: KIND_ICON["function"],
        rows: rows(
          (t.functions?.crypto ?? []).map((op) => ({
            kind: "function" as const,
            id: op,
            label: op,
            detail: "local",
          })),
        ),
      },
      {
        title: "Model Insight",
        color: KIND_COLOR["insight"],
        icon: KIND_ICON["insight"],
        rows: rows(
          t.insight?.actions?.length
            ? [
                {
                  kind: "insight" as const,
                  id: "insight",
                  label: "Model Insight",
                  detail: "advisory (ADR-0005)",
                  sampleFamily: t.insight.sample_family ?? "insight",
                },
              ]
            : [],
        ),
      },
    ];
  });

  ngOnInit(): void {
    this.reload();
    this.scenarioApi.formats("iso8583").subscribe({
      next: (f) => this.formats.set(f),
      error: () => this.formats.set([]),
    });
    this.scenarioApi.catalog().subscribe({
      next: (c) => this.catalog.set(c),
      error: () => this.catalog.set([]),
    });
    this.scenarioApi.projects().subscribe({
      next: (p) => this.projects.set(p),
      error: () => this.projects.set([]),
    });
    this.loadHistory();
  }

  /** How many history entries a save would promote (ticked, or all). */
  readonly promoteCount = computed(() => {
    const n = this.picked().size;
    return n > 0 ? n : this.history().length;
  });

  isPicked(seq: number): boolean {
    return this.picked().has(seq);
  }

  togglePick(seq: number): void {
    this.picked.update((set) => {
      const next = new Set(set);
      next.has(seq) ? next.delete(seq) : next.add(seq);
      return next;
    });
  }

  clearPicks(): void {
    this.picked.set(new Set());
  }

  /** Default the action to the family's first catalog action (or send_message),
   *  and clear any custom-action override. Called when the target changes. */
  private syncActionToFamily(): void {
    this.customAction.set(false);
    const first = this.catalogActions()[0]?.name;
    this.action.set(first ?? "send_message");
  }

  /** The <select> changed: either a real action or the custom sentinel. */
  pickAction(value: string): void {
    if (value === "__custom__") {
      this.customAction.set(true);
      return;
    }
    this.customAction.set(false);
    this.action.set(value);
  }

  /** Insert a payload skeleton from the chosen action's catalog payload_hint,
   *  so the operator edits values instead of recalling field names. */
  prefillPayload(): void {
    const spec = this.catalogActions().find((a) => a.name === this.action());
    if (!spec) return;
    const skeleton: Record<string, unknown> = {};
    for (const [k, type] of Object.entries(spec.payload_hint ?? {})) {
      const t = String(type).toLowerCase();
      skeleton[k] = t.startsWith("int")
        ? 0
        : t.startsWith("json") || t.startsWith("object")
          ? {}
          : "";
    }
    this.payloadText.set(JSON.stringify(skeleton, null, 2));
  }

  reload(): void {
    this.api.targets().subscribe({
      next: (t) => this.targets.set(t),
      error: () => this.ui.toast("error", "Could not load playground targets"),
    });
  }

  iconFor(kind: string): string {
    return KIND_ICON[kind] ?? "list";
  }

  kindColor(kind: string | undefined): string {
    return KIND_COLOR[kind ?? ""] ?? "var(--brand)";
  }

  isSelected(row: TargetRow): boolean {
    const s = this.selected();
    return !!s && s.kind === row.kind && s.id === row.id;
  }

  select(row: TargetRow): void {
    this.selected.set(row);
    this.environment.set("");
    this.result.set(null);
    this.appliedSample.set(null);
    if (row.kind === "function") {
      this.customAction.set(false);
      this.action.set(row.id);
      // seed the operation's runnable sample so it fires green as-is.
      const sample = (this.targets()?.samples?.functions?.crypto ?? []).find(
        (s) => s.action === row.id,
      );
      if (sample) {
        this.applySample(sample);
      } else {
        this.payloadText.set("{}");
      }
    } else {
      this.syncActionToFamily();
      // seed the family's first sample when there is one, else the old default.
      const first = this.samplesFor()[0];
      if (first) {
        this.applySample(first);
      } else {
        this.payloadText.set(this.isoLike() ? ISO_SAMPLE : "{}");
      }
    }
  }

  /** Apply a ready-made sample: action + payload land in the composer,
   *  ready to edit or fire. */
  applySample(s: PlaygroundSample): void {
    const sel = this.selected();
    if (sel && sel.kind !== "function") {
      const known = this.catalogActions().some((a) => a.name === s.action);
      this.customAction.set(!known && this.catalogActions().length > 0);
      this.action.set(s.action);
    }
    this.payloadText.set(JSON.stringify(s.payload, null, 2));
    this.appliedSample.set(s.name);
  }

  selectRaw(): void {
    this.selected.set({
      kind: "raw",
      id: "raw",
      label: "Raw endpoint",
      detail: "",
      protocol: this.rawProtocol,
    });
    this.environment.set("");
    this.result.set(null);
    this.appliedSample.set(null);
    this.syncActionToFamily();
    const first = this.samplesFor()[0];
    if (first) {
      this.applySample(first);
    } else {
      this.payloadText.set(this.isoLike() ? ISO_SAMPLE : "{}");
    }
  }

  /** Raw protocol changed → the family (and its actions/samples) changes. */
  onRawProtocolChange(): void {
    if (this.selected()?.kind === "raw") {
      this.selected.update((s) =>
        s ? { ...s, protocol: this.rawProtocol } : s,
      );
      this.appliedSample.set(null);
      this.syncActionToFamily();
      const first = this.samplesFor()[0];
      if (first) this.applySample(first);
    }
  }

  /** Manual payload edits drop the applied-sample highlight — it is no longer
   *  the sample, it is the operator's message now. */
  onPayloadEdit(text: string): void {
    this.payloadText.set(text);
    this.appliedSample.set(null);
  }

  private targetRef(): PlaygroundTargetRef | null {
    const sel = this.selected();
    if (!sel) return null;
    if (sel.kind === "raw") {
      if (!this.rawHost || !this.rawPort) {
        this.ui.toast("error", "Raw target needs host and port");
        return null;
      }
      // payShield is its own adapter (host-command client), not a TCP dialect.
      if (this.rawProtocol === "payshield") {
        return {
          kind: "raw",
          host: this.rawHost,
          port: this.rawPort,
          adapter: "payshield",
        };
      }
      return {
        kind: "raw",
        host: this.rawHost,
        port: this.rawPort,
        protocol: this.rawProtocol,
      };
    }
    if (sel.kind === "function") return { kind: "function" };
    if (sel.kind === "insight") return { kind: "insight" };
    const ref: PlaygroundTargetRef = { kind: sel.kind, id: sel.id };
    if (sel.kind === "connection" && this.environment()) {
      ref.environment = this.environment();
    }
    return ref;
  }

  execute(): void {
    const target = this.targetRef();
    if (!target || this.payloadError()) return;
    this.busy.set(true);
    this.api
      .execute({
        target,
        action: this.action() || "send_message",
        payload: JSON.parse(this.payloadText() || "{}"),
        message_format_id: this.formatId() || null,
        // a pristine (unedited) sample labels the history entry after itself
        label: this.appliedSample() || null,
      })
      .subscribe({
        next: (r) => {
          this.busy.set(false);
          this.result.set(r);
          this.loadHistory();
        },
        error: (err) => {
          this.busy.set(false);
          this.ui.toast(
            "error",
            err?.error?.detail ??
              "Execute failed — is the target reference valid?",
          );
        },
      });
  }

  loadHistory(): void {
    this.api.history().subscribe({
      next: (h) => this.history.set(h.rows),
      error: () => {},
    });
  }

  refire(seq: number): void {
    this.busy.set(true);
    this.api.refire(seq).subscribe({
      next: (r) => {
        this.busy.set(false);
        this.result.set(r);
        this.loadHistory();
      },
      error: () => {
        this.busy.set(false);
        this.ui.toast("error", "Re-fire failed");
      },
    });
  }

  inspect(row: PlaygroundHistoryRow): void {
    this.result.set({
      ok: row.ok,
      status: row.status,
      target: row.target,
      request: row.request,
      response: row.response,
      error: row.error,
      duration_ms: row.duration_ms,
      history_seq: row.seq,
    });
  }

  clearHistory(): void {
    this.api.clearHistory().subscribe({
      next: () => {
        this.history.set([]);
        this.picked.set(new Set());
      },
      error: () => this.ui.toast("error", "Could not clear history"),
    });
  }

  /** Open the promote dialog (name + project + which entries). */
  openSave(): void {
    if (!this.history().length) return;
    this.saving.set(true);
  }

  cancelSave(): void {
    this.saving.set(false);
  }

  /** Promote the ticked entries (or all, if none ticked) into a scenario. */
  confirmSave(): void {
    const name = this.saveName.trim();
    if (!name) {
      this.ui.toast("error", "Give the scenario a name");
      return;
    }
    const picks = [...this.picked()];
    this.api
      .saveAsScenario({
        name,
        seqs: picks.length ? picks : undefined,
        project_id: this.saveProject || undefined,
      })
      .subscribe({
        next: (res) => {
          this.saving.set(false);
          this.ui.toast(
            "success",
            `Saved scenario "${res.name}" (${res.steps} steps)`,
          );
          this.router.navigate(["/constructor", res.scenario_id]);
        },
        error: (err) =>
          this.ui.toast(
            "error",
            err?.error?.detail ?? "Save as scenario failed",
          ),
      });
  }

  describeTarget(t: Record<string, unknown> | undefined): string {
    if (!t) return "";
    const kind = String(t["kind"] ?? "");
    const id = t["id"] ? String(t["id"]) : "";
    const env = t["environment"] ? `@${t["environment"]}` : "";
    if (kind === "raw") return `${t["host"] ?? "?"}:${t["port"] ?? "?"}`;
    if (kind === "function") return "local function";
    if (kind === "insight") return "model insight";
    return `${id}${env}` || kind;
  }

  pretty(v: unknown): string {
    if (v === null || v === undefined) return "—";
    try {
      return JSON.stringify(v, null, 2);
    } catch {
      return String(v);
    }
  }
}
