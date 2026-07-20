import {
  Component,
  OnInit,
  OnDestroy,
  inject,
  signal,
  computed,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { RouterLink } from "@angular/router";

import { forkJoin } from "rxjs";

import {
  RunApiService,
  SavedSimulator,
  Simulator,
  SimulatorMetrics,
  SimulatorLogLine,
  CaptureSnapshot,
} from "../run-monitor/run-api.service";
import { UiService } from "../shared/ui.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { StaleChipComponent } from "../shared/ui/stale-chip.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";
import { KpiCardComponent } from "../shared/ui/kpi-card.component";
import { CodeEditorComponent } from "../shared/code-editor.component";

const TEMPLATE = `{
  "protocol": "iso8583",
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii" },
  "rules": [
    { "when": { "mti": "0200", "de": { "4": { "gte": "000000100000" } } },
      "respond": { "echo": ["11","37"], "set": { "39": "61" } } },
    { "when": { "mti": "0200", "de": { "2": { "prefix": "400000" } } },
      "respond": { "echo": ["11","37"], "set": { "39": "05" } } }
  ],
  "default": { "echo": ["11","37"], "set": { "39": "00", "38": "AUTH99" } }
}`;

/** Same as TEMPLATE but with a chaos/fault-injection block: ~8% of replies are
 *  dropped, a quarter get 50–400ms of latency, and a small share come back
 *  malformed or truncated — for resilience testing beyond the happy path. */
const CHAOS_TEMPLATE = `{
  "protocol": "iso8583",
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii" },
  "chaos": {
    "seed": 1,
    "drop_pct": 8,
    "latency_ms": { "min": 50, "max": 400, "pct": 25 },
    "malformed_pct": 5,
    "malformed_mode": "garbage",
    "partial_pct": 3,
    "partial_bytes": "half"
  },
  "rules": [
    { "when": { "mti": "0200", "de": { "4": { "gte": "000000100000" } } },
      "respond": { "echo": ["11","37"], "set": { "39": "61" } } }
  ],
  "default": { "echo": ["11","37"], "set": { "39": "00", "38": "AUTH99" } }
}`;

/** Thales payShield 10K HSM simulator: answers host commands (NC, A0, BU,
 *  CW/CY, CA, EC, M6/M8) with real payment crypto under a test LMK — be the
 *  HSM with no physical appliance. Listens on the classic host port 1500. */
const HSM_TEMPLATE = `{
  "protocol": "payshield",
  "host": "0.0.0.0",
  "port": 1500,
  "header_bytes": 4,
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii" },
  "lmk": "0123456789ABCDEFFEDCBA9876543210",
  "firmware": "0007-E000",
  "commands": {}
}`;

/** VISA Base I-style scheme simulator: handles authorization (0100/0200),
 *  reversal & advice (0400/0420), network management (0800) and clearing
 *  advice (0220/0520) over ISO 8583. Stand-in (STIP) approves auths unless a
 *  limit / BIN / expiry rule declines. Functional fidelity from public ISO
 *  8583 — not a byte-exact copy of VISA's confidential field specs. */
const VISA_TEMPLATE = `{
  "protocol": "visa",
  "host": "0.0.0.0",
  "port": 7010,
  "message_format_id": "visa-base1",
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii" },
  "visa": {
    "stand_in": true,
    "approve_code": "00",
    "decline_over": "000000100000",
    "block_bins": ["400000"],
    "block_response": "05",
    "expiry_check": true
  },
  "rules": []
}`;

/** CyberSource / Visa Acceptance REST gateway simulator: answers the Payments
 *  REST API (POST /pts/v2/payments + captures / refunds / reversals / voids)
 *  with the public response envelope (status AUTHORIZED / PARTIAL_AUTHORIZED /
 *  DECLINED, processorInformation, errorInformation) — be the gateway with no
 *  real CyberSource account. Functional fidelity from the public REST contract,
 *  not a byte-exact copy of processor-specific response codes. */
const CYBERSOURCE_TEMPLATE = `{
  "protocol": "cybersource",
  "host": "0.0.0.0",
  "port": 8080,
  "cybersource": {
    "merchant_id": "payprobe_test",
    "require_auth": true,
    "approve_response": "100",
    "decline_over": 100000,
    "partial_over": 50000,
    "decline_response": "200",
    "block_bins": ["400000"],
    "expiry_check": true,
    "cvn_decline_values": ["999"],
    "avs_code": "Y",
    "cvn_result": "M"
  },
  "rules": []
}`;

/** Stripe PaymentIntents PSP simulator (ADR-0009): answers POST
 *  /v1/payment_intents (+ confirm / capture / cancel), /v1/refunds and
 *  retrievals with Stripe's status machine, error envelope and the documented
 *  test-card ladder (4242… succeeds, …0002 declines, …9995 insufficient funds,
 *  …3155 requires 3DS action) — be Stripe with no real account. Bodies are
 *  form-encoded with bracket notation, exactly like the real API (JSON is
 *  tolerated). Functional fidelity, not a byte-exact PaymentIntent. */
const STRIPE_TEMPLATE = `{
  "protocol": "stripe",
  "host": "0.0.0.0",
  "port": 8085,
  "stripe": {
    "require_auth": true,
    "decline_over": null,
    "default_currency": "usd"
  },
  "webhooks": {
    "url": "http://127.0.0.1:9101/webhooks/stripe",
    "secret": "whsec_test",
    "events": [],
    "chaos": {"drop_pct": 0, "latency_ms": 0, "malformed_pct": 0}
  },
  "rules": []
}`;

/** Adyen Checkout PSP simulator (ADR-0009): answers POST /payments (+
 *  /payments/details, /payments/{ref}/refunds) with Adyen's resultCode value
 *  set, the documented holderName refusal triggers (NOT_ENOUGH_BALANCE,
 *  CARD_EXPIRED, …), the 3DS2 ChallengeShopper test card and async refund
 *  accept — be Adyen test with no Customer Area. Webhook notifications are
 *  HMAC-signed per the documented colon-joined scheme; the template secret is
 *  the hex of "payprobe-hmac-key", matching the pack's receiver flow. */
const ADYEN_TEMPLATE = `{
  "protocol": "adyen",
  "host": "0.0.0.0",
  "port": 8086,
  "adyen": {
    "require_auth": true,
    "merchant_account": null,
    "decline_over": null
  },
  "webhooks": {
    "url": "http://127.0.0.1:9102/webhooks/adyen",
    "secret": "70617970726f62652d686d61632d6b6579",
    "events": [],
    "chaos": {"drop_pct": 0, "latency_ms": 0, "malformed_pct": 0}
  },
  "rules": []
}`;

/** PayPal Orders v2 PSP simulator (ADR-0009): answers /v1/oauth2/token,
 *  /v2/checkout/orders (+ capture) and /v2/payments/captures/{id}/refund with
 *  the Orders status machine and the documented negative-testing header
 *  (PayPal-Mock-Response → INSTRUMENT_DECLINED etc.) — be the PayPal sandbox
 *  with no developer account. Webhook events carry transmission headers with
 *  an honestly-labelled HMAC stand-in signature. */
const PAYPAL_TEMPLATE = `{
  "protocol": "paypal",
  "host": "0.0.0.0",
  "port": 8087,
  "paypal": {
    "require_auth": true,
    "strict_tokens": false,
    "decline_over": null
  },
  "webhooks": {
    "url": "http://127.0.0.1:9103/webhooks/paypal",
    "secret": "payprobe-paypal-webhook",
    "events": [],
    "chaos": {"drop_pct": 0, "latency_ms": 0, "malformed_pct": 0}
  },
  "rules": []
}`;

/** Transparent TCP proxy / tap: sit between a real client and a real upstream
 *  host, relaying frames byte-for-byte while recording (redacted) every message.
 *  Set `upstream` to the real host; captured traffic can be saved as a scenario.
 *  Stage 1 supports `mode: "tap"` (observe only). */
const PROXY_TEMPLATE = `{
  "kind": "proxy",
  "protocol": "iso8583",
  "host": "0.0.0.0",
  "port": 7020,
  "mode": "tap",
  "framing": { "length_prefix_bytes": 2, "length_byte_order": "big", "encoding": "ascii" },
  "upstream": { "host": "10.0.1.50", "port": 7000, "connect_timeout_sec": 10 },
  "capture": {
    "enabled": true,
    "max_frames": 5000,
    "store_raw": false,
    "redact": true,
    "mode": "all",
    "rules": [
      { "when": { "de": { "39": { "ne": "00" } } },
        "log": { "tag": "declined", "level": "warn" } },
      { "when": { "mti": "0200", "de": { "4": { "gte": "000000100000" } } },
        "log": { "tag": "high-value" } }
    ]
  }
}`;

/** NATS responder (ADR-0006): connect out to the broker and subscribe to
 *  subjects (optionally in a queue group), decoding each message through the
 *  codec and answering request/reply messages with a templated body. Binds no
 *  port — set `host`/`port` (or `servers`) to the broker, not a listen port. */
const NATS_TEMPLATE = `{
  "protocol": "nats",
  "host": "nats-1",
  "port": 4222,
  "subjects": ["pay.auth"],
  "queue_group": "issuers",
  "codec": "json",
  "rules": [
    { "when": { "subject": { "eq": "pay.auth" }, "body": { "amount": { "gte": 100000 } } },
      "respond": { "json": { "decision": "DECLINE", "reason": "limit" } } }
  ],
  "default": { "json": { "decision": "APPROVE" } }
}`;

interface Breakdown {
  key: string;
  count: number;
  pct: number;
}

/** Saved host/responder simulators — define rules-driven ISO 8583 / HSM
 *  responders once, then start / stop / clear them and watch live traffic
 *  metrics, the same way the load page surfaces a run. */
@Component({
  selector: "app-simulators",
  standalone: true,
  imports: [
    PageHeaderComponent,
    ButtonComponent,
    IconComponent,
    BadgeComponent,
    KpiCardComponent,
    CodeEditorComponent,
    FormsModule,
    RouterLink,
    StaleChipComponent,
  ],
  template: `
    <div class="sm">
      <pp-page-header
        subtitle="Saved host/responder simulators — be the switch, acquirer or HSM"
      >
        <pp-stale-chip [health]="pollHealth" />
        <pp-button
          variant="secondary"
          size="sm"
          icon="activity"
          (click)="reload()"
        >
          Refresh
        </pp-button>
        <pp-button
          [variant]="showNew() ? 'ghost' : 'primary'"
          size="sm"
          [icon]="showNew() ? 'x' : 'plus'"
          (click)="toggleNew()"
        >
          {{ showNew() ? "Close" : "New simulator" }}
        </pp-button>
      </pp-page-header>

      <div class="sm__body">
        <!-- KPI summary -->
        <section class="kpis">
          <pp-kpi-card
            label="Running"
            [value]="totals().running"
            icon="cpu"
            [caption]="totals().saved + ' saved'"
          />
          <pp-kpi-card
            label="Throughput"
            [value]="totals().rps"
            icon="activity"
            caption="requests/sec across all"
          />
          <pp-kpi-card
            label="Messages received"
            [value]="totals().received"
            icon="layers"
            caption="while running"
          />
          <pp-kpi-card
            label="Faults injected"
            [value]="totals().faults"
            icon="zap"
            [caption]="totals().faults ? 'chaos active' : 'none yet'"
          />
        </section>

        <!-- New / edit simulator form -->
        @if (showNew()) {
          <div class="form">
            <div class="form__head">
              <h3>{{ editingId() ? "Edit simulator" : "New simulator" }}</h3>
              <div class="presets">
                <span class="presets__label">Preset</span>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'basic'"
                  (click)="usePreset('basic')"
                >
                  Basic
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'chaos'"
                  (click)="usePreset('chaos')"
                >
                  <pp-icon name="zap" [size]="13" [strokeWidth]="2.5" /> Chaos
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'hsm'"
                  (click)="usePreset('hsm')"
                >
                  <pp-icon name="cpu" [size]="13" [strokeWidth]="2.5" />
                  payShield 10K
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'visa'"
                  (click)="usePreset('visa')"
                >
                  <pp-icon name="shield" [size]="13" [strokeWidth]="2.5" /> VISA
                  scheme
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'cybersource'"
                  (click)="usePreset('cybersource')"
                >
                  <pp-icon name="send" [size]="13" [strokeWidth]="2.5" />
                  CyberSource REST
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'stripe'"
                  (click)="usePreset('stripe')"
                >
                  <pp-icon name="send" [size]="13" [strokeWidth]="2.5" />
                  Stripe PSP
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'adyen'"
                  (click)="usePreset('adyen')"
                >
                  <pp-icon name="send" [size]="13" [strokeWidth]="2.5" />
                  Adyen PSP
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'paypal'"
                  (click)="usePreset('paypal')"
                >
                  <pp-icon name="send" [size]="13" [strokeWidth]="2.5" />
                  PayPal PSP
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'nats'"
                  (click)="usePreset('nats')"
                >
                  <pp-icon name="activity" [size]="13" [strokeWidth]="2.5" />
                  NATS responder
                </button>
                <button
                  type="button"
                  class="seg"
                  [class.seg--on]="preset() === 'proxy'"
                  (click)="usePreset('proxy')"
                >
                  <pp-icon name="up" [size]="13" [strokeWidth]="2.5" /> TCP
                  Proxy / Tap
                </button>
              </div>
            </div>

            <div class="grid">
              <label class="f">
                <span>Label</span>
                <input [(ngModel)]="label" placeholder="switch-sim" />
              </label>
              <label class="f">
                <span>Listen port (0 = pick a free port)</span>
                <input type="number" [(ngModel)]="port" />
              </label>
            </div>

            <label class="f">
              <span>Responder config (JSON)</span>
              <app-code-editor
                language="json"
                [value]="config"
                [height]="300"
                (valueChange)="config = $event"
              />
            </label>

            <label class="check">
              <input type="checkbox" [(ngModel)]="enabled" />
              <span>Auto-start this simulator when the orchestrator boots</span>
            </label>

            @if (preset() === "chaos") {
              <p class="hint">
                <pp-icon name="zap" [size]="14" [strokeWidth]="2.5" />
                Chaos preset injects latency, drops, malformed frames and
                partial reads — great for resilience testing beyond the happy
                path.
              </p>
            }

            @if (preset() === "proxy") {
              <p class="hint">
                <pp-icon name="up" [size]="14" [strokeWidth]="2.5" />
                Proxy sits between a real client and the
                <code>upstream</code> host. <code>mode: "tap"</code> relays
                byte-for-byte and records (redacted) traffic;
                <code>"intercept"</code> mutates/injects matched frames in
                flight (add top-level <code>rules</code>);
                <code>"stub"</code> answers matched requests locally and
                forwards the rest. Point your client at this listen port; open
                <b>Capture</b> on the running card to view or save it as a
                scenario.
              </p>
            }

            <div class="form__actions">
              <pp-button variant="secondary" icon="check" (click)="save(false)">
                {{ editingId() ? "Save changes" : "Save" }}
              </pp-button>
              <pp-button variant="primary" icon="play" (click)="save(true)">
                {{ editingId() ? "Save & start" : "Save & start" }}
              </pp-button>
            </div>
          </div>
        }

        <!-- Simulator list -->
        @if (loading()) {
          <div class="state">
            <span class="muted">Loading…</span>
          </div>
        } @else if (!sims().length) {
          <div class="state state--empty">
            <span class="state__icon"><pp-icon name="cpu" [size]="28" /></span>
            <h3>No simulators saved</h3>
            <p class="muted">
              Save a responder definition, then start it whenever you need a
              host to test against — and watch its live traffic metrics.
            </p>
            <pp-button variant="primary" icon="plus" (click)="toggleNew()">
              New simulator
            </pp-button>
          </div>
        } @else {
          <section class="cards">
            @for (s of sims(); track s.id) {
              <article
                class="card"
                [class.card--chaos]="s.chaos"
                [class.card--off]="!s.running"
              >
                <header class="card__head">
                  <span class="dot" [class.dot--off]="!s.running"></span>
                  <div class="card__title">
                    <b>{{ s.label }}</b>
                    <span class="muted mono">{{ s.id }}</span>
                  </div>
                  <div class="card__tags">
                    <pp-badge [tone]="s.running ? 'success' : 'neutral'">
                      {{ s.running ? "running" : "stopped" }}
                    </pp-badge>
                    <pp-badge tone="info">{{ s.protocol }}</pp-badge>
                    @if (s.unsaved) {
                      <pp-badge tone="warning">unsaved</pp-badge>
                    }
                    @if (s.chaos) {
                      <pp-badge tone="warning">chaos</pp-badge>
                    }
                    @if (s.playground_hits) {
                      <pp-badge
                        tone="warning"
                        title="Ad-hoc Playground messages have hit this simulator — its stats are contaminated for certification"
                      >
                        {{ s.playground_hits }} playground
                      </pp-badge>
                    }
                  </div>
                </header>

                <div class="stats">
                  <div class="stat">
                    <span class="stat__k">Port</span>
                    <span class="stat__v mono">{{
                      s.running ? s.port : "—"
                    }}</span>
                  </div>
                  <div class="stat">
                    <span class="stat__k">Rules</span>
                    <span class="stat__v">{{ s.rules }}</span>
                  </div>
                  <div class="stat">
                    <span class="stat__k">Req/s</span>
                    <span class="stat__v">{{
                      s.running ? s.rps || 0 : "—"
                    }}</span>
                  </div>
                  <div class="stat">
                    <span class="stat__k">Received</span>
                    <span class="stat__v">{{
                      s.running ? s.received || 0 : "—"
                    }}</span>
                  </div>
                  <div class="stat">
                    <span class="stat__k">Faults</span>
                    <span class="stat__v" [class.stat__v--warn]="s.faults">
                      {{ s.running ? s.faults || 0 : "—" }}
                    </span>
                  </div>
                </div>

                @if (!s.unsaved) {
                  <label class="autostart">
                    <input
                      type="checkbox"
                      [checked]="s.enabled"
                      (change)="toggleEnabled(s, $event)"
                    />
                    <span>Auto-start on boot</span>
                  </label>
                } @else {
                  <p class="unsaved-note muted">
                    Running but not saved — started outside the registry. Save
                    it to keep it on this page and auto-start it on boot.
                  </p>
                }

                <footer class="card__foot">
                  @if (s.unsaved) {
                    <pp-button
                      variant="primary"
                      size="sm"
                      icon="check"
                      (click)="saveAdhoc(s)"
                    >
                      Save
                    </pp-button>
                    <pp-button
                      variant="secondary"
                      size="sm"
                      icon="x"
                      (click)="stop(s)"
                    >
                      Stop
                    </pp-button>
                    <pp-button
                      variant="ghost"
                      size="sm"
                      icon="undo"
                      (click)="clear(s)"
                    >
                      Clear stats
                    </pp-button>
                  } @else if (s.running) {
                    <pp-button
                      variant="secondary"
                      size="sm"
                      icon="x"
                      (click)="stop(s)"
                    >
                      Stop
                    </pp-button>
                    <pp-button
                      variant="ghost"
                      size="sm"
                      icon="undo"
                      (click)="clear(s)"
                    >
                      Clear stats
                    </pp-button>
                  } @else {
                    <pp-button
                      variant="primary"
                      size="sm"
                      icon="play"
                      (click)="start(s)"
                    >
                      Start
                    </pp-button>
                    <pp-button
                      variant="ghost"
                      size="sm"
                      icon="format"
                      (click)="edit(s)"
                    >
                      Edit
                    </pp-button>
                  }
                  <pp-button
                    variant="ghost"
                    size="sm"
                    icon="chart"
                    (click)="toggleMetrics(s)"
                  >
                    {{ openMetrics() === s.id ? "Hide metrics" : "Metrics" }}
                  </pp-button>
                  @if (s.proxy && s.running) {
                    <pp-button
                      variant="ghost"
                      size="sm"
                      icon="search"
                      (click)="toggleCapture(s)"
                    >
                      {{ openCapture() === s.id ? "Hide capture" : "Capture" }}
                    </pp-button>
                  }
                  <a class="link" [routerLink]="['/simulators', s.id]">
                    <pp-icon name="up" [size]="13" /> Open
                  </a>
                  @if (!s.unsaved) {
                    <button
                      class="del"
                      type="button"
                      (click)="remove(s)"
                      title="Delete"
                    >
                      <pp-icon name="x" [size]="14" />
                    </button>
                  }
                </footer>

                <!-- Inline live metrics -->
                @if (openMetrics() === s.id) {
                  @if (!s.running) {
                    <div class="metrics metrics--empty">
                      <span class="muted"
                        >Start the simulator to see live metrics.</span
                      >
                    </div>
                  } @else if (metrics(); as m) {
                    <div class="metrics">
                      <div class="spark">
                        <div class="spark__head">
                          <span class="spark__title">Requests / sec</span>
                          <span class="spark__now">{{ s.rps || 0 }}/s</span>
                        </div>
                        <svg viewBox="0 0 320 64" preserveAspectRatio="none">
                          <polyline
                            class="spark__line"
                            [attr.points]="rpsPath()"
                            fill="none"
                          />
                        </svg>
                        <div class="spark__foot muted">
                          avg {{ m.stats.avg_rps }}/s · {{ m.stats.elapsed_s }}s
                          window
                        </div>
                      </div>

                      <div class="bd">
                        <span class="bd__title">Response codes</span>
                        @if (!rcBreakdown().length) {
                          <span class="muted bd__empty">No replies yet.</span>
                        }
                        @for (b of rcBreakdown(); track b.key) {
                          <div class="bar">
                            <span class="bar__k mono">{{ b.key }}</span>
                            <span class="bar__track">
                              <span
                                class="bar__fill"
                                [style.width.%]="b.pct"
                              ></span>
                            </span>
                            <span class="bar__v">{{ b.count }}</span>
                          </div>
                        }
                      </div>

                      <div class="bd">
                        <span class="bd__title">Message types</span>
                        @if (!mtiBreakdown().length) {
                          <span class="muted bd__empty">No traffic yet.</span>
                        }
                        @for (b of mtiBreakdown(); track b.key) {
                          <div class="bar">
                            <span class="bar__k mono">{{ b.key }}</span>
                            <span class="bar__track">
                              <span
                                class="bar__fill bar__fill--alt"
                                [style.width.%]="b.pct"
                              ></span>
                            </span>
                            <span class="bar__v">{{ b.count }}</span>
                          </div>
                        }
                      </div>

                      <div class="log">
                        <span class="bd__title">Recent traffic</span>
                        @if (!m.log?.length) {
                          <span class="muted bd__empty">
                            No traffic yet — point a step's connection at port
                            {{ s.port }}.
                          </span>
                        }
                        @for (
                          line of (m.log || []).slice(-8).reverse();
                          track $index
                        ) {
                          <code class="logline">
                            @if (line.mti) {
                              <span class="logline__mti"
                                >MTI {{ line.mti }}</span
                              >
                              <span class="logline__de">{{
                                deSummary(line.de)
                              }}</span>
                            } @else {
                              <span class="logline__mti">{{
                                line.command
                              }}</span>
                              <span class="logline__de"
                                >hdr {{ line.header }}</span
                              >
                            }
                            @if (line.chaos) {
                              <span class="fault">⚡ {{ line.chaos }}</span>
                            }
                          </code>
                        }
                      </div>
                    </div>
                  }
                }

                <!-- Inline proxy capture -->
                @if (openCapture() === s.id) {
                  <div class="capture">
                    <div class="capture__head">
                      <span class="bd__title">
                        Captured traffic
                        @if (capture(); as c) {
                          <span class="muted">
                            · {{ c.count }} frames{{
                              c.dropped ? " · " + c.dropped + " dropped" : ""
                            }}{{
                              c.mode === "matched" && c.skipped
                                ? " · " + c.skipped + " unmatched"
                                : ""
                            }}{{ c.enabled ? "" : " · capture off" }}
                          </span>
                        }
                      </span>
                      <div class="capture__actions">
                        <input
                          class="capture__name"
                          [(ngModel)]="captureName"
                          placeholder="scenario name"
                        />
                        <pp-button
                          variant="secondary"
                          size="sm"
                          icon="check"
                          (click)="saveCapture(s)"
                        >
                          Save as scenario
                        </pp-button>
                        <pp-button
                          variant="ghost"
                          size="sm"
                          icon="undo"
                          (click)="clearCapture(s)"
                        >
                          Clear
                        </pp-button>
                      </div>
                    </div>
                    @if (captureTags().length) {
                      <div class="cap__filters">
                        <button
                          type="button"
                          class="cap__chip"
                          [class.cap__chip--on]="!captureTagFilter()"
                          (click)="captureTagFilter.set(null)"
                        >
                          all
                        </button>
                        @for (t of captureTags(); track t) {
                          <button
                            type="button"
                            class="cap__chip"
                            [class.cap__chip--on]="captureTagFilter() === t"
                            (click)="captureTagFilter.set(t)"
                          >
                            {{ t }}
                          </button>
                        }
                      </div>
                    }
                    @if (capture(); as c) {
                      @if (!c.rows.length) {
                        <span class="muted bd__empty">
                          No traffic captured yet — point a client at port
                          {{ s.port }}.
                        </span>
                      }
                    }
                    @for (row of captureView(); track row.seq) {
                      <code
                        class="logline"
                        [class.logline--warn]="
                          row.level === 'warn' || row.level === 'error'
                        "
                      >
                        <span
                          class="cap__dir"
                          [class.cap__dir--in]="row.direction === 'c2u'"
                        >
                          {{ row.direction === "c2u" ? "→ host" : "← client" }}
                        </span>
                        @if (row.undecoded) {
                          <span class="logline__de">(undecoded frame)</span>
                        } @else {
                          <span class="logline__mti">MTI {{ row.mti }}</span>
                          <span class="logline__de">{{
                            deSummary(row.values)
                          }}</span>
                        }
                        @if (row.tag) {
                          <span
                            class="cap__tag"
                            [class.cap__tag--warn]="
                              row.level === 'warn' || row.level === 'error'
                            "
                            [title]="row.note || ''"
                            >{{ row.tag }}</span
                          >
                        }
                      </code>
                    }
                  </div>
                }
              </article>
            }
          </section>
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
        background: var(--bg-base);
        color: var(--text-primary);
      }
      .sm {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .sm__body {
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
      }

      .kpis {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: var(--space-4);
      }

      /* Form */
      .form {
        border: 1px solid var(--border);
        border-radius: var(--radius-xl);
        padding: var(--space-5);
        background: var(--bg-surface);
        box-shadow: var(--shadow-xs);
        display: flex;
        flex-direction: column;
        gap: var(--space-4);
      }
      .form__head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--space-4);
      }
      .form__head h3 {
        margin: 0;
        font-size: var(--text-base);
      }
      .presets {
        display: inline-flex;
        align-items: center;
        gap: var(--space-1-5);
      }
      .presets__label {
        font-size: var(--text-xs);
        color: var(--text-muted);
        margin-right: var(--space-1);
      }
      .seg {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: var(--space-1-5) var(--space-3);
        border: 1px solid var(--border-strong);
        background: var(--bg-subtle);
        color: var(--text-secondary);
        border-radius: var(--radius-md);
        font: inherit;
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
        cursor: pointer;
        transition:
          color var(--duration-fast) var(--ease-out),
          border-color var(--duration-fast) var(--ease-out),
          background var(--duration-fast) var(--ease-out);
      }
      .seg:hover {
        color: var(--brand);
        border-color: var(--brand);
      }
      .seg--on {
        background: color-mix(in srgb, var(--brand) 12%, transparent);
        border-color: var(--brand);
        color: var(--brand);
      }
      .grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--space-3) var(--space-4);
      }
      .f {
        display: flex;
        flex-direction: column;
        gap: var(--space-1-5);
        font-size: var(--text-sm);
      }
      .f span {
        color: var(--text-muted);
        font-size: var(--text-xs);
      }
      .f input {
        padding: var(--space-2) var(--space-3);
        border: 1px solid var(--border-strong);
        border-radius: var(--radius-md);
        background: var(--bg-base);
        color: var(--text-primary);
        font: inherit;
        font-size: var(--text-sm);
      }
      .f input:focus {
        outline: none;
        border-color: var(--brand);
        box-shadow: 0 0 0 3px var(--focus-ring);
      }
      .check,
      .autostart {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-sm);
        color: var(--text-secondary);
        cursor: pointer;
      }
      .autostart {
        font-size: var(--text-xs);
        color: var(--text-muted);
      }
      .hint {
        display: flex;
        align-items: flex-start;
        gap: var(--space-2);
        margin: 0;
        padding: var(--space-2-5) var(--space-3);
        border-radius: var(--radius-md);
        background: color-mix(in srgb, var(--color-warning) 12%, transparent);
        color: var(--color-warning);
        font-size: var(--text-sm);
        line-height: 1.4;
      }
      .form__actions {
        display: flex;
        justify-content: flex-end;
        gap: var(--space-2);
      }

      /* States */
      .state {
        padding: var(--space-6);
      }
      .state--empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        gap: var(--space-3);
        padding: var(--space-8) var(--space-6);
        border: 1px dashed var(--border-strong);
        border-radius: var(--radius-xl);
        background: var(--bg-surface);
      }
      .state--empty h3 {
        margin: 0;
        font-size: var(--text-lg);
      }
      .state--empty p {
        max-width: 44ch;
        margin: 0;
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

      /* Cards */
      .cards {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
        gap: var(--space-4);
        align-items: start;
      }
      .card {
        position: relative;
        display: flex;
        flex-direction: column;
        gap: var(--space-3-5);
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-xl);
        box-shadow: var(--shadow-xs);
        padding: var(--space-5);
        transition: box-shadow var(--duration-normal) var(--ease-out);
      }
      .card:hover {
        box-shadow: var(--shadow-md);
      }
      .card--off {
        opacity: 0.92;
      }
      .card--chaos {
        border-color: color-mix(
          in srgb,
          var(--color-warning) 45%,
          var(--border)
        );
      }
      .card--chaos::before {
        content: "";
        position: absolute;
        inset: 0 0 auto 0;
        height: 3px;
        border-radius: var(--radius-xl) var(--radius-xl) 0 0;
        background: var(--color-warning);
        opacity: 0.85;
      }
      .card__head {
        display: flex;
        align-items: flex-start;
        gap: var(--space-2-5);
      }
      .dot {
        flex: none;
        width: 9px;
        height: 9px;
        margin-top: 5px;
        border-radius: 50%;
        background: var(--color-success);
        box-shadow: 0 0 0 4px
          color-mix(in srgb, var(--color-success) 22%, transparent);
      }
      .dot--off {
        background: var(--text-muted);
        box-shadow: none;
      }
      .card__title {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
      }
      .card__title b {
        font-size: var(--text-base);
        font-weight: var(--weight-semibold);
      }
      .card__title .mono {
        font-size: var(--text-xs);
      }
      .card__tags {
        margin-left: auto;
        display: flex;
        flex-direction: column;
        align-items: flex-end;
        gap: var(--space-1);
      }
      .stats {
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: var(--space-2);
        padding: var(--space-3) 0;
        border-top: 1px solid var(--border);
        border-bottom: 1px solid var(--border);
      }
      .stat {
        display: flex;
        flex-direction: column;
        gap: 2px;
      }
      .stat__k {
        font-size: var(--text-xs);
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
      }
      .stat__v {
        font-size: var(--text-base);
        font-weight: var(--weight-semibold);
        color: var(--text-primary);
      }
      .stat__v--warn {
        color: var(--color-warning);
      }
      .card__foot {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: var(--space-2);
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
      .del {
        margin-left: auto;
        display: inline-grid;
        place-items: center;
        width: 30px;
        height: 30px;
        border: 1px solid var(--border-strong);
        background: var(--bg-base);
        color: var(--text-muted);
        border-radius: var(--radius-md);
        cursor: pointer;
        transition:
          color var(--duration-fast) var(--ease-out),
          border-color var(--duration-fast) var(--ease-out);
      }
      .del:hover {
        color: var(--color-danger);
        border-color: var(--color-danger);
      }

      /* Inline metrics */
      .metrics {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: var(--space-3);
        padding: var(--space-3);
        border-radius: var(--radius-md);
        background: var(--bg-subtle);
      }
      .metrics--empty {
        display: block;
        text-align: center;
        padding: var(--space-4);
      }
      .spark {
        grid-column: 1 / -1;
        display: flex;
        flex-direction: column;
        gap: var(--space-1);
      }
      .spark__head {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
      }
      .spark__title {
        font-size: var(--text-xs);
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
      }
      .spark__now {
        font-size: var(--text-sm);
        font-weight: var(--weight-semibold);
        color: var(--brand);
      }
      .spark svg {
        width: 100%;
        height: 64px;
      }
      .spark__line {
        stroke: var(--brand);
        stroke-width: 2;
        vector-effect: non-scaling-stroke;
      }
      .spark__foot {
        font-size: var(--text-xs);
      }
      .bd {
        display: flex;
        flex-direction: column;
        gap: var(--space-1-5);
      }
      .bd__title {
        font-size: var(--text-xs);
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
      }
      .bd__empty {
        font-size: var(--text-xs);
      }
      .bar {
        display: grid;
        grid-template-columns: 44px 1fr auto;
        align-items: center;
        gap: var(--space-2);
        font-size: var(--text-xs);
      }
      .bar__k {
        color: var(--text-secondary);
      }
      .bar__track {
        height: 8px;
        border-radius: var(--radius-full);
        background: var(--bg-base);
        overflow: hidden;
      }
      .bar__fill {
        display: block;
        height: 100%;
        border-radius: var(--radius-full);
        background: var(--brand);
      }
      .bar__fill--alt {
        background: color-mix(
          in srgb,
          var(--brand) 55%,
          var(--color-info, #38bdf8)
        );
      }
      .bar__v {
        color: var(--text-primary);
        font-weight: var(--weight-semibold);
      }
      .log {
        grid-column: 1 / -1;
        display: flex;
        flex-direction: column;
        gap: 4px;
        max-height: 180px;
        overflow: auto;
      }
      .logline {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: var(--text-xs);
        white-space: nowrap;
      }
      .logline__mti {
        color: var(--brand);
        font-weight: var(--weight-semibold);
      }
      .logline__de {
        color: var(--text-secondary);
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .fault {
        margin-left: auto;
        flex: none;
        padding: 1px 7px;
        border-radius: var(--radius-full);
        font-size: 10px;
        background: color-mix(in srgb, var(--color-warning) 20%, transparent);
        color: var(--color-warning);
      }
      .capture {
        grid-column: 1 / -1;
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-top: var(--space-3);
        padding-top: var(--space-3);
        border-top: 1px solid var(--border-subtle, var(--border));
        max-height: 260px;
        overflow: auto;
      }
      .capture__head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--space-3);
        flex-wrap: wrap;
      }
      .capture__actions {
        display: flex;
        align-items: center;
        gap: var(--space-2);
      }
      .capture__name {
        padding: 4px 8px;
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        background: var(--bg-base);
        color: var(--text-primary);
        font-size: var(--text-xs);
      }
      .cap__dir {
        flex: none;
        padding: 1px 7px;
        border-radius: var(--radius-full);
        font-size: 10px;
        font-weight: var(--weight-semibold);
        background: color-mix(in srgb, var(--text-secondary) 18%, transparent);
        color: var(--text-secondary);
      }
      .cap__dir--in {
        background: color-mix(in srgb, var(--brand) 20%, transparent);
        color: var(--brand);
      }
      .cap__filters {
        display: flex;
        flex-wrap: wrap;
        gap: var(--space-2);
        margin-bottom: 2px;
      }
      .cap__chip {
        padding: 2px 9px;
        border: 1px solid var(--border);
        border-radius: var(--radius-full);
        background: transparent;
        color: var(--text-secondary);
        font-size: 11px;
        cursor: pointer;
      }
      .cap__chip--on {
        background: color-mix(in srgb, var(--brand) 18%, transparent);
        border-color: var(--brand);
        color: var(--brand);
      }
      .cap__tag {
        margin-left: auto;
        flex: none;
        padding: 1px 7px;
        border-radius: var(--radius-full);
        font-size: 10px;
        background: color-mix(in srgb, var(--brand) 16%, transparent);
        color: var(--brand);
      }
      .cap__tag--warn {
        background: color-mix(in srgb, var(--color-warning) 20%, transparent);
        color: var(--color-warning);
      }
      .logline--warn .logline__mti {
        color: var(--color-warning);
      }

      @media (max-width: 760px) {
        .kpis,
        .grid {
          grid-template-columns: 1fr 1fr;
        }
        .metrics {
          grid-template-columns: 1fr;
        }
      }
    `,
  ],
})
export class SimulatorsComponent implements OnInit, OnDestroy {
  private readonly api = inject(RunApiService);
  private readonly ui = inject(UiService);

  readonly sims = signal<SavedSimulator[]>([]);
  readonly loading = signal(true);
  readonly showNew = signal(false);
  readonly editingId = signal<string | null>(null);
  readonly openMetrics = signal<string | null>(null);
  readonly metrics = signal<SimulatorMetrics | null>(null);
  readonly openCapture = signal<string | null>(null);
  readonly capture = signal<CaptureSnapshot | null>(null);
  readonly captureTagFilter = signal<string | null>(null);
  /** Distinct tags present in the current capture (for the filter chips). */
  readonly captureTags = computed(() => {
    const rows = this.capture()?.rows ?? [];
    return Array.from(
      new Set(rows.map((r) => r.tag).filter((t): t is string => !!t)),
    ).sort();
  });
  /** Most recent 40 capture rows, newest first, after the tag filter. */
  readonly captureView = computed(() => {
    const c = this.capture();
    if (!c) return [];
    const f = this.captureTagFilter();
    const rows = f ? c.rows.filter((r) => r.tag === f) : c.rows;
    return rows.slice(-40).reverse();
  });
  readonly preset = signal<
    | "basic"
    | "chaos"
    | "hsm"
    | "visa"
    | "cybersource"
    | "stripe"
    | "adyen"
    | "paypal"
    | "nats"
    | "proxy"
  >("basic");

  readonly pollHealth = new PollHealth();
  private readonly poll = new PagePoll(2000, () => this.tick());

  readonly totals = computed(() => {
    const s = this.sims();
    const running = s.filter((x) => x.running);
    return {
      saved: s.length,
      running: running.length,
      rps: Math.round(running.reduce((a, x) => a + (x.rps || 0), 0) * 10) / 10,
      received: running.reduce((a, x) => a + (x.received || 0), 0),
      faults: running.reduce((a, x) => a + (x.faults || 0), 0),
    };
  });

  readonly rpsPath = computed(() => {
    const pts = this.metrics()?.samples ?? [];
    if (!pts.length) return "";
    const max = Math.max(1, ...pts.map((p) => p.rps));
    const n = Math.max(1, pts.length - 1);
    return pts
      .map((p, i) => {
        const x = (i / n) * 320;
        const y = 62 - (p.rps / max) * 58;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  });

  readonly rcBreakdown = computed(() =>
    this.breakdown(this.metrics()?.stats.by_response_code),
  );
  readonly mtiBreakdown = computed(() =>
    this.breakdown(this.metrics()?.stats.by_mti),
  );

  label = "switch-sim";
  port = 0;
  config = TEMPLATE;
  enabled = false;
  captureName = "";

  ngOnInit() {
    this.reload();
    // Light poll so running stats and the open metrics panel stay live
    // (skips ticks while the tab is hidden).
    this.poll.start();
  }

  ngOnDestroy() {
    this.poll.stop();
    this.pollHealth.destroy();
  }

  private breakdown(map?: Record<string, number>): Breakdown[] {
    if (!map) return [];
    const entries = Object.entries(map);
    const total = entries.reduce((a, [, v]) => a + v, 0) || 1;
    return entries
      .map(([key, count]) => ({ key, count, pct: (count / total) * 100 }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 6);
  }

  toggleNew() {
    if (this.showNew()) {
      this.showNew.set(false);
      this.editingId.set(null);
    } else {
      this.resetForm();
      this.showNew.set(true);
    }
  }

  private resetForm() {
    this.editingId.set(null);
    this.label = "switch-sim";
    this.port = 0;
    this.config = TEMPLATE;
    this.enabled = false;
    this.preset.set("basic");
  }

  usePreset(
    which:
      | "basic"
      | "chaos"
      | "hsm"
      | "visa"
      | "cybersource"
      | "stripe"
      | "adyen"
      | "paypal"
      | "nats"
      | "proxy",
  ) {
    this.preset.set(which);
    if (which === "hsm") {
      this.config = HSM_TEMPLATE;
      if (this.label === "switch-sim") this.label = "payshield-hsm";
    } else if (which === "visa") {
      this.config = VISA_TEMPLATE;
      if (this.label === "switch-sim") this.label = "visa-scheme";
    } else if (which === "cybersource") {
      this.config = CYBERSOURCE_TEMPLATE;
      if (this.label === "switch-sim") this.label = "cybersource-rest";
    } else if (which === "stripe") {
      this.config = STRIPE_TEMPLATE;
      if (this.label === "switch-sim") this.label = "stripe-psp";
    } else if (which === "adyen") {
      this.config = ADYEN_TEMPLATE;
      if (this.label === "switch-sim") this.label = "adyen-psp";
    } else if (which === "paypal") {
      this.config = PAYPAL_TEMPLATE;
      if (this.label === "switch-sim") this.label = "paypal-psp";
    } else if (which === "nats") {
      this.config = NATS_TEMPLATE;
      if (this.label === "switch-sim") this.label = "nats-responder";
    } else if (which === "proxy") {
      this.config = PROXY_TEMPLATE;
      if (this.label === "switch-sim") this.label = "tcp-proxy";
    } else {
      this.config = which === "chaos" ? CHAOS_TEMPLATE : TEMPLATE;
    }
  }

  reload() {
    this.loading.set(true);
    forkJoin({
      saved: this.api.savedSimulators(),
      running: this.api.simulators(),
    }).subscribe({
      next: ({ saved, running }) => {
        this.sims.set(this.mergeSims(saved, running));
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  /** Periodic refresh: list (cheap) + the open metrics panel, if any. */
  private tick() {
    if (
      !this.sims().some((s) => s.running) &&
      !this.openMetrics() &&
      !this.openCapture()
    )
      return;
    forkJoin({
      saved: this.api.savedSimulators(),
      running: this.api.simulators(),
    }).subscribe({
      next: ({ saved, running }) => {
        this.sims.set(this.mergeSims(saved, running));
        this.pollHealth.markOk();
      },
      error: () => this.pollHealth.markError(),
    });
    const id = this.openMetrics();
    if (id) this.refreshMetrics(id);
    const cid = this.openCapture();
    if (cid) this.refreshCapture(cid);
  }

  /** Saved configs plus any running ad-hoc simulators (started via MCP/API)
   *  that have no saved config yet — so a started simulator is never hidden. */
  private mergeSims(
    saved: SavedSimulator[],
    running: Simulator[],
  ): SavedSimulator[] {
    const savedIds = new Set(saved.map((s) => s.id));
    const adhoc: SavedSimulator[] = running
      .filter((r) => !savedIds.has(r.id))
      .map((r) => ({
        id: r.id,
        label: r.label,
        config: {},
        enabled: false,
        protocol: r.protocol,
        rules: r.rules,
        chaos: !!r.chaos,
        running: true,
        unsaved: true,
        port: r.port,
        received: r.received,
        faults: r.faults,
        rps: r.rps,
        playground_hits: r.playground_hits,
      }));
    return [...saved, ...adhoc];
  }

  private refreshMetrics(id: string) {
    this.api.simulatorMetrics(id).subscribe({
      next: (m) => this.metrics.set(m),
      error: () => {},
    });
  }

  save(thenStart: boolean) {
    let cfg: Record<string, unknown>;
    try {
      cfg = JSON.parse(this.config);
    } catch {
      this.ui.toast("error", "Config is not valid JSON.");
      return;
    }
    cfg["port"] = this.port || 0;
    cfg["host"] = cfg["host"] ?? "0.0.0.0";
    const label = this.label.trim() || "simulator";
    const editing = this.editingId();

    const done = (s: SavedSimulator) => {
      this.ui.toast("success", editing ? "Saved." : "Simulator saved.");
      this.showNew.set(false);
      this.editingId.set(null);
      if (thenStart) {
        this.api.startSavedSimulator(s.id).subscribe({
          next: () => {
            this.ui.toast("success", "Simulator started.");
            this.reload();
          },
          error: () => {
            this.ui.toast("error", "Saved, but could not start.");
            this.reload();
          },
        });
      } else {
        this.reload();
      }
    };

    if (editing) {
      this.api
        .updateSavedSimulator(editing, {
          label,
          config: cfg,
          enabled: this.enabled,
        })
        .subscribe({
          next: done,
          error: () => this.ui.toast("error", "Save failed."),
        });
    } else {
      this.api
        .createSavedSimulator({ label, config: cfg, enabled: this.enabled })
        .subscribe({
          next: done,
          error: () => this.ui.toast("error", "Save failed."),
        });
    }
  }

  edit(s: SavedSimulator) {
    this.editingId.set(s.id);
    this.label = s.label;
    const cfg = { ...s.config } as Record<string, unknown>;
    this.port = Number(cfg["port"]) || 0;
    this.enabled = s.enabled;
    this.preset.set(s.chaos ? "chaos" : "basic");
    this.config = JSON.stringify(s.config, null, 2);
    this.showNew.set(true);
  }

  start(s: SavedSimulator) {
    this.api.startSavedSimulator(s.id).subscribe({
      next: () => {
        this.ui.toast("success", "Simulator started.");
        this.reload();
      },
      error: () =>
        this.ui.toast("error", "Could not start — port in use or bad config?"),
    });
  }

  /** Persist a running ad-hoc simulator into the saved registry (keeps it
   *  running). After this it's a normal saved card. */
  saveAdhoc(s: SavedSimulator) {
    this.api.saveRunningSimulator(s.id, true).subscribe({
      next: () => {
        this.ui.toast("success", "Saved — it'll auto-start on boot.");
        this.reload();
      },
      error: () => this.ui.toast("error", "Save failed."),
    });
  }

  async stop(s: SavedSimulator) {
    const ok = await this.ui.confirm(`Stop simulator “${s.label}”?`, {
      title: "Stop simulator",
      confirmLabel: "Stop",
      danger: true,
    });
    if (!ok) return;
    const onStop = {
      next: () => {
        this.ui.toast("success", "Stopped.");
        this.reload();
      },
      error: () => this.ui.toast("error", "Stop failed."),
    };
    // unsaved (ad-hoc) sims aren't in the saved registry — stop the instance.
    if (s.unsaved) {
      this.api.stopSimulator(s.id).subscribe(onStop);
    } else {
      this.api.stopSavedSimulator(s.id).subscribe(onStop);
    }
  }

  clear(s: SavedSimulator) {
    this.api.clearSimulator(s.id).subscribe({
      next: () => {
        this.ui.toast("success", "Stats cleared.");
        if (this.openMetrics() === s.id) this.refreshMetrics(s.id);
        this.reload();
      },
      error: () => this.ui.toast("error", "Could not clear stats."),
    });
  }

  toggleEnabled(s: SavedSimulator, ev: Event) {
    const enabled = (ev.target as HTMLInputElement).checked;
    this.api.setSavedSimulatorEnabled(s.id, enabled).subscribe({
      next: () => this.reload(),
      error: () => this.ui.toast("error", "Could not update auto-start."),
    });
  }

  async remove(s: SavedSimulator) {
    const ok = await this.ui.confirm(
      `Delete saved simulator “${s.label}”?` +
        (s.running ? " It will be stopped first." : ""),
      { title: "Delete simulator", confirmLabel: "Delete", danger: true },
    );
    if (!ok) return;
    this.api.deleteSavedSimulator(s.id).subscribe({
      next: () => {
        this.ui.toast("success", "Deleted.");
        if (this.openMetrics() === s.id) this.openMetrics.set(null);
        this.reload();
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }

  toggleMetrics(s: SavedSimulator) {
    if (this.openMetrics() === s.id) {
      this.openMetrics.set(null);
      this.metrics.set(null);
      return;
    }
    this.openMetrics.set(s.id);
    this.metrics.set(null);
    if (s.running) this.refreshMetrics(s.id);
  }

  deSummary(de?: Record<string, string>): string {
    if (!de) return "";
    return Object.entries(de)
      .slice(0, 6)
      .map(([k, v]) => `DE${k}=${v}`)
      .join("  ");
  }

  // -- proxy capture --------------------------------------------------------

  toggleCapture(s: SavedSimulator) {
    if (this.openCapture() === s.id) {
      this.openCapture.set(null);
      this.capture.set(null);
      return;
    }
    this.openCapture.set(s.id);
    this.capture.set(null);
    this.captureTagFilter.set(null);
    if (!this.captureName) this.captureName = `${s.label}-capture`;
    this.refreshCapture(s.id);
  }

  private refreshCapture(id: string) {
    this.api.capture(id).subscribe({
      next: (c) => this.capture.set(c),
      error: () => this.capture.set(null),
    });
  }

  saveCapture(s: SavedSimulator) {
    const name = (this.captureName || "").trim();
    if (!name) {
      this.ui.toast("error", "Enter a scenario name first");
      return;
    }
    this.api.saveCaptureAsScenario(s.id, { name }).subscribe({
      next: (r) =>
        this.ui.toast(
          "success",
          `Saved scenario “${r.name}” (${r.steps} step${r.steps === 1 ? "" : "s"})`,
        ),
      error: (e) =>
        this.ui.toast(
          "error",
          e?.error?.detail || "Could not save capture as scenario",
        ),
    });
  }

  clearCapture(s: SavedSimulator) {
    this.api.clearCapture(s.id).subscribe({
      next: () => this.refreshCapture(s.id),
      error: () => this.ui.toast("error", "Could not clear capture"),
    });
  }
}
