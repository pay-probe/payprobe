import { CommonModule } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  Input,
  OnChanges,
  OnDestroy,
  OnInit,
  SimpleChanges,
  computed,
  inject,
  signal,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import {
  AgentHubApiService,
  HeartbeatDetail,
  HeartbeatStep,
  HeartbeatSummary,
  problemsOf,
} from "../shared/agent-hub-api.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { UiService } from "../shared/ui.service";
import { StaleChipComponent } from "../shared/ui/stale-chip.component";

/** Heartbeats finish in seconds; a slow poll here would hide the outcome. */
const POLL_MS = 5_000;

interface WaterfallRow {
  key: string;
  step: HeartbeatStep;
  offsetMs: number;
  leftPct: number;
  widthPct: number;
  /** ok | bad | run | plan, drives the bar colour. */
  cls: string;
  label: string;
}

/**
 * Heartbeats of one agent (ADR-0010 phase 2): the bounded wakes the runner
 * recorded, newest first, with the step waterfall of the selected one. Plan-mode
 * writes appear as a proposed plan, never as executed calls. Cancel asks the
 * runner to stop before its next LLM or tool call; Revert restores every
 * journalled write under the caller's own token (reverting is the human's act).
 * The list polls through PollHealth so a dead agent-hub is never mistaken for
 * "no heartbeats yet".
 */
@Component({
  selector: "app-agent-heartbeats",
  standalone: true,
  imports: [CommonModule, FormsModule, StaleChipComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="hb">
      <div class="hb__head">
        <h3>Heartbeats</h3>
        <span class="muted">{{ rows().length }} recorded</span>
        <pp-stale-chip [health]="health" />
        @if (running(); as r) {
          <span class="pill pill--running"
            >running since {{ r.started_at | date: "mediumTime" }}</span
          >
        }
      </div>

      <!-- ---------------------------------------------- wake -->
      <div class="wake">
        <textarea
          class="mono"
          rows="2"
          [(ngModel)]="wakeInput"
          [disabled]="!canInvoke || waking()"
          placeholder="Input for this wake (optional). The agent's instructions decide what it does with it."
          spellcheck="false"
        ></textarea>
        <button
          class="btn btn--primary"
          [disabled]="!canInvoke || waking()"
          [title]="
            canInvoke
              ? running()
                ? 'A heartbeat is already running: this wake coalesces with it'
                : 'Run one bounded heartbeat now (wake = manual)'
              : 'Needs a role in the agent\\'s rbac.invoke (or admin)'
          "
          (click)="wake()"
        >
          {{ waking() ? "Waking…" : "Wake now" }}
        </button>
      </div>

      <!-- ---------------------------------------------- list -->
      @if (loading()) {
        <p class="muted">Loading heartbeats…</p>
      } @else if (!rows().length) {
        <p class="muted">No heartbeats yet for this agent.</p>
      } @else {
        <table class="hbl">
          <thead>
            <tr>
              <th>started</th>
              <th>status</th>
              <th>wake</th>
              <th>v</th>
              <th>by</th>
              <th title="LLM turns + tool calls">steps</th>
              <th title="Plan-mode writes recorded, not executed">proposed</th>
              <th title="Journalled writes (revertable)">writes</th>
              <th>tokens</th>
            </tr>
          </thead>
          <tbody>
            @for (h of rows(); track h.id) {
              <tr
                [class.selected]="h.id === selectedId()"
                (click)="select(h.id)"
              >
                <td class="muted">{{ h.started_at | date: "short" }}</td>
                <td>
                  <span [class]="'pill pill--' + h.status">{{
                    h.status.replace("_", " ")
                  }}</span>
                  @if (h.reverted_at) {
                    <span
                      class="pill pill--reverted"
                      title="Writes were reverted"
                      >reverted</span
                    >
                  }
                </td>
                <td class="muted">{{ h.wake }}</td>
                <td class="mono">{{ h.version }}</td>
                <td class="muted">{{ h.invoked_by || "—" }}</td>
                <td class="mono">{{ h.n_steps }}</td>
                <td class="mono">{{ h.n_proposed || "" }}</td>
                <td class="mono">{{ h.n_writes || "" }}</td>
                <td class="mono muted">{{ h.tokens_in + h.tokens_out }}</td>
              </tr>
            }
          </tbody>
        </table>
      }

      <!-- ---------------------------------------------- detail -->
      @if (detail(); as d) {
        <div class="hbd">
          <div class="hbd__head">
            <div>
              <span [class]="'pill pill--' + d.status">{{
                d.status.replace("_", " ")
              }}</span>
              <span class="mono muted hbd__id" [title]="d.id">{{
                d.id | slice: 0 : 12
              }}</span>
              <span class="muted">
                · {{ d.wake }} wake by {{ d.invoked_by || "?" }} · v{{
                  d.version
                }}
                · spec {{ d.spec_sha256 | slice: 0 : 12 }}
                @if (d.model) {
                  · {{ d.model }}
                }
                · {{ d.tokens_in }} in / {{ d.tokens_out }} out
                @if (durationMs(); as ms) {
                  · {{ ms }} ms
                }
              </span>
            </div>
            <div class="actions">
              @if (d.status === "running") {
                <button
                  class="btn btn--danger"
                  [disabled]="!canInvoke || d.cancel_requested"
                  (click)="cancel(d)"
                  title="Stop before the next LLM or tool call"
                >
                  {{ d.cancel_requested ? "Cancel requested" : "Cancel" }}
                </button>
              } @else if (d.journal.length && !d.reverted_at) {
                <button
                  class="btn btn--danger"
                  [disabled]="!isAdmin"
                  (click)="revert(d)"
                  title="Restore every journalled write, newest first (admin)"
                >
                  Revert {{ d.journal.length }} write{{
                    d.journal.length === 1 ? "" : "s"
                  }}
                </button>
              } @else if (d.reverted_at) {
                <span class="muted"
                  >reverted {{ d.reverted_at | date: "short" }}</span
                >
              }
              <button class="btn" (click)="detail.set(null)">Close</button>
            </div>
          </div>

          @if (d.error) {
            <div class="hbd__error">{{ d.error }}</div>
          }

          @if (d.input) {
            <h4 class="tsub">input</h4>
            <pre class="hbd__pre">{{ d.input }}</pre>
          }

          @if (d.proposed.length) {
            <h4 class="tsub">
              proposed plan · {{ d.proposed.length }} call{{
                d.proposed.length === 1 ? "" : "s"
              }}
              not executed
            </h4>
            <ol class="plan">
              @for (p of d.proposed; track $index) {
                <li>
                  <span class="mono plan__tool">{{ p.tool }}</span>
                  <span class="muted">step {{ p.step }}</span>
                  <pre class="hbd__pre">{{ json(p.args) }}</pre>
                </li>
              }
            </ol>
          }

          <h4 class="tsub">
            steps · {{ d.steps.length }}
            @if (totalMs() > 0) {
              · span {{ totalMs() }} ms
            }
          </h4>
          @if (!d.steps.length) {
            <p class="muted">
              @if (d.status === "running") {
                Waiting for the first LLM turn…
              } @else {
                No steps were recorded.
              }
            </p>
          }
          @for (t of waterfall(); track t.key) {
            <div class="trow" [class]="'trow--' + t.cls">
              <button class="trow__head" (click)="toggle(t.key)">
                <span class="trow__chev">{{
                  expanded().has(t.key) ? "▾" : "▸"
                }}</span>
                <span class="trow__n mono">{{ t.step.n }}</span>
                <span [class]="'pill pill--kind-' + t.step.kind">{{
                  t.step.kind
                }}</span>
                <span class="trow__label mono">{{ t.label }}</span>
                <span
                  class="wf"
                  [title]="t.step.ms + 'ms @ +' + t.offsetMs + 'ms'"
                >
                  <span
                    class="wf__bar"
                    [class]="'wf__bar wf__bar--' + t.cls"
                    [style.left.%]="t.leftPct"
                    [style.width.%]="t.widthPct"
                  ></span>
                </span>
                <span class="trow__ms muted">{{ t.step.ms }}ms</span>
                <span [class]="'trow__st fg--' + t.cls">{{
                  status(t.step)
                }}</span>
              </button>
              @if (expanded().has(t.key)) {
                <div class="trow__body">
                  @if (t.step.kind === "llm") {
                    @if (t.step.text) {
                      <pre class="hbd__pre">{{ t.step.text }}</pre>
                    }
                    @if (t.step.tool_calls.length) {
                      <div class="tsub">asked for</div>
                      @for (c of t.step.tool_calls; track $index) {
                        <div class="call">
                          <span class="mono">{{ c.name }}</span>
                          <pre class="hbd__pre">{{ json(c.args) }}</pre>
                        </div>
                      }
                    }
                    <div class="muted small">
                      usage so far: {{ t.step.usage.input }} in /
                      {{ t.step.usage.output }} out
                    </div>
                  } @else if (t.step.kind === "postcheck") {
                    <div class="tsub">
                      {{
                        !t.step.ok
                          ? "no evidence: the model's claim was kept as written"
                          : t.step.changed
                            ? "the answer was corrected from the platform's evidence"
                            : "the platform's evidence confirmed the answer"
                      }}
                    </div>
                    <div class="muted small">
                      run {{ t.step.run_id }} · claimed
                      <span class="mono">{{ json(t.step.claimed) }}</span>
                      @if (t.step.ok) {
                        · verified
                        <span class="mono">{{ json(t.step.verified) }}</span>
                        · history says
                        <span class="mono">{{ t.step.verdict }}</span>
                      }
                    </div>
                    @if (t.step.error) {
                      <pre class="hbd__pre hbd__pre--err">{{
                        t.step.error
                      }}</pre>
                    }
                  } @else {
                    <div class="tsub">args</div>
                    <pre class="hbd__pre">{{ json(t.step.args) }}</pre>
                    @if (t.step.error) {
                      <div class="tsub">
                        {{
                          t.step.proposed
                            ? "recorded as proposed"
                            : t.step.guardrail
                              ? "refused by guardrail"
                              : "error"
                        }}
                      </div>
                      <pre class="hbd__pre hbd__pre--err">{{
                        t.step.error
                      }}</pre>
                    }
                    @if (t.step.truncated) {
                      <div class="muted small">
                        result was capped before reaching the model
                      </div>
                    }
                  }
                </div>
              }
            </div>
          }

          @if (d.result) {
            <h4 class="tsub">result</h4>
            <pre class="hbd__pre">{{ d.result }}</pre>
          }
        </div>
      }
    </section>
  `,
  styles: [
    `
      .hb {
        display: flex;
        flex-direction: column;
        gap: 10px;
        border-top: 1px solid var(--pp-border, #e5e7eb);
        padding-top: 12px;
      }
      .muted {
        color: var(--pp-text-muted, #6b7280);
      }
      .small {
        font-size: 12px;
      }
      .mono {
        font-family: var(--pp-font-mono, ui-monospace, monospace);
      }
      h3 {
        margin: 0;
        font-size: 15px;
      }
      .hb__head {
        display: flex;
        gap: 10px;
        align-items: center;
      }
      .wake {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 8px;
        align-items: start;
      }
      textarea {
        font: inherit;
        padding: 6px 8px;
        border: 1px solid var(--pp-border, #e5e7eb);
        border-radius: 6px;
        background: var(--pp-input-bg, transparent);
        color: inherit;
        width: 100%;
        box-sizing: border-box;
      }
      textarea.mono {
        font-family: var(--pp-font-mono, ui-monospace, monospace);
        font-size: 12.5px;
      }
      .hbl {
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
      }
      .hbl th,
      .hbl td {
        text-align: left;
        padding: 5px 8px;
        border-bottom: 1px solid var(--pp-border, #e5e7eb);
        white-space: nowrap;
      }
      .hbl tbody tr {
        cursor: pointer;
      }
      .hbl tr.selected td {
        background: var(--pp-row-selected, rgba(83, 51, 237, 0.06));
      }
      .hbd {
        border: 1px solid var(--pp-border, #e5e7eb);
        border-radius: 10px;
        padding: 12px 14px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        font-size: 13px;
      }
      .hbd__head {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: flex-start;
        flex-wrap: wrap;
      }
      .hbd__id {
        margin-left: 6px;
      }
      .actions {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
      }
      .hbd__error {
        border: 1px solid #dc2626;
        border-radius: 8px;
        padding: 6px 10px;
      }
      .hbd__pre {
        margin: 2px 0;
        padding: 6px 8px;
        border-radius: 6px;
        background: var(--pp-surface-soft, rgba(127, 127, 127, 0.08));
        font-size: 12px;
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 260px;
        overflow: auto;
      }
      .hbd__pre--err {
        border: 1px solid #dc2626;
      }
      .tsub {
        margin: 8px 0 2px;
        font-size: 11.5px;
        color: var(--pp-text-muted, #6b7280);
        text-transform: uppercase;
        letter-spacing: 0.02em;
      }
      .plan {
        margin: 0;
        padding-left: 22px;
      }
      .plan li {
        margin-bottom: 4px;
      }
      .plan__tool {
        font-weight: 600;
        margin-right: 8px;
      }
      .call {
        margin-bottom: 4px;
      }
      /* waterfall (same geometry as the run trace view) */
      .trow {
        border: 1px solid var(--pp-border, #e5e7eb);
        border-radius: 8px;
        overflow: hidden;
      }
      .trow + .trow {
        margin-top: 4px;
      }
      .trow__head {
        display: grid;
        grid-template-columns: 14px 24px 44px minmax(120px, 220px) 1fr 58px 90px;
        gap: 8px;
        align-items: center;
        width: 100%;
        padding: 5px 8px;
        border: 0;
        background: transparent;
        color: inherit;
        font: inherit;
        text-align: left;
        cursor: pointer;
      }
      .trow__label {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .trow__ms {
        text-align: right;
      }
      .trow__st {
        text-transform: lowercase;
        font-size: 12px;
      }
      .trow__body {
        padding: 6px 10px 10px;
        border-top: 1px solid var(--pp-border, #e5e7eb);
        background: var(--pp-surface-soft, rgba(127, 127, 127, 0.05));
      }
      .wf {
        position: relative;
        height: 10px;
        background: var(--color-bg-soft, rgba(127, 127, 127, 0.12));
        border-radius: 5px;
        overflow: hidden;
      }
      .wf__bar {
        position: absolute;
        top: 0;
        height: 100%;
        min-width: 2px;
        border-radius: 5px;
        background: var(--color-muted, #9ca3af);
      }
      .wf__bar--ok {
        background: var(--color-accent, #0c9b6f);
      }
      .wf__bar--bad {
        background: var(--color-fail, #dc2626);
      }
      .wf__bar--run {
        background: var(--color-info, #4f7cf0);
      }
      .wf__bar--plan {
        background: var(--color-warn, #b4690e);
      }
      .fg--ok {
        color: #15803d;
      }
      .fg--bad {
        color: #b91c1c;
      }
      .fg--run {
        color: #4f7cf0;
      }
      .fg--plan {
        color: #b45309;
      }
      .pill {
        display: inline-block;
        padding: 1px 8px;
        border-radius: 999px;
        font-size: 11.5px;
        border: 1px solid var(--pp-border, #e5e7eb);
        text-transform: lowercase;
      }
      .pill--done {
        border-color: #16a34a;
        color: #15803d;
      }
      .pill--running {
        border-color: #4f7cf0;
        color: #4f7cf0;
      }
      .pill--failed,
      .pill--timed_out,
      .pill--budget_exceeded,
      .pill--quota_exceeded {
        border-color: #dc2626;
        color: #b91c1c;
      }
      .pill--cancelled,
      .pill--paused,
      .pill--reverted {
        border-color: #d97706;
        color: #b45309;
      }
      .pill--kind-llm {
        border-color: var(--brand, #5333ed);
        color: var(--brand, #5333ed);
      }
      .pill--kind-tool {
        opacity: 0.85;
      }
      .pill--kind-postcheck {
        border-color: #d97706;
        color: #b45309;
      }
      @media (max-width: 900px) {
        .trow__head {
          grid-template-columns: 14px 24px 44px 1fr 58px;
        }
        .wf,
        .trow__st {
          display: none;
        }
      }
    `,
  ],
})
export class AgentHeartbeatsComponent implements OnInit, OnChanges, OnDestroy {
  private readonly api = inject(AgentHubApiService);
  private readonly ui = inject(UiService);

  /** Agent name; the list is scoped to it. */
  @Input({ required: true }) agent!: string;
  /** Caller may wake and cancel (agent's rbac.invoke or admin). */
  @Input() canInvoke = false;
  /** Caller may revert (admin only). */
  @Input() isAdmin = false;

  readonly health = new PollHealth();
  readonly rows = signal<HeartbeatSummary[]>([]);
  readonly loading = signal(true);
  readonly detail = signal<HeartbeatDetail | null>(null);
  readonly expanded = signal(new Set<string>());
  readonly waking = signal(false);
  wakeInput = "";

  readonly selectedId = computed(() => this.detail()?.id ?? null);
  readonly running = computed(
    () => this.rows().find((h) => h.status === "running") ?? null,
  );
  readonly totalMs = computed(() =>
    (this.detail()?.steps ?? []).reduce((acc, s) => acc + (s.ms || 0), 0),
  );
  readonly durationMs = computed(() => {
    const d = this.detail();
    if (!d?.finished_at) return null;
    const ms = Date.parse(d.finished_at) - Date.parse(d.started_at);
    return Number.isFinite(ms) && ms >= 0 ? ms : null;
  });

  /**
   * Steps carry durations, not timestamps: the runner is strictly sequential,
   * so a cumulative layout is the exact timeline, not an approximation.
   */
  readonly waterfall = computed<WaterfallRow[]>(() => {
    const steps = this.detail()?.steps ?? [];
    const span = Math.max(this.totalMs(), 1);
    let cursor = 0;
    return steps.map((step, i) => {
      const offsetMs = cursor;
      cursor += step.ms || 0;
      return {
        key: `${step.n}:${i}`,
        step,
        offsetMs,
        leftPct: (offsetMs / span) * 100,
        widthPct: Math.max(((step.ms || 0) / span) * 100, 1.5),
        cls: this.cls(step),
        label:
          step.kind === "tool"
            ? step.tool
            : step.kind === "postcheck"
              ? `post-check · ${step.tool}`
              : step.tool_calls.length
                ? `llm → ${step.tool_calls.map((c) => c.name).join(", ")}`
                : "llm · final answer",
      };
    });
  });

  private readonly poll = new PagePoll(POLL_MS, () => this.tick());

  ngOnInit(): void {
    this.poll.start();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes["agent"] && !changes["agent"].firstChange) {
      this.rows.set([]);
      this.loading.set(true);
      this.detail.set(null);
      this.expanded.set(new Set());
      this.tick();
    }
  }

  ngOnDestroy(): void {
    this.poll.stop();
    this.health.destroy();
  }

  /** One beat: the list, plus the open record while it is still running. */
  private tick(): void {
    this.api.heartbeats(this.agent).subscribe({
      next: (rows) => {
        this.rows.set(rows);
        this.loading.set(false);
        this.health.markOk();
        const open = this.detail();
        if (open) {
          const listed = rows.find((h) => h.id === open.id);
          // refresh while running, and once more when the list says it finished
          if (
            open.status === "running" ||
            (listed && listed.status !== open.status)
          ) {
            this.refresh(open.id);
          }
        }
      },
      error: () => {
        this.loading.set(false);
        this.health.markError();
      },
    });
  }

  select(id: string): void {
    if (this.selectedId() === id) return;
    this.expanded.set(new Set());
    this.refresh(id);
  }

  toggle(key: string): void {
    const next = new Set(this.expanded());
    if (next.has(key)) next.delete(key);
    else next.add(key);
    this.expanded.set(next);
  }

  wake(): void {
    this.waking.set(true);
    this.api.wake(this.agent, { input: this.wakeInput.trim() }).subscribe({
      next: (hb) => {
        this.waking.set(false);
        if (hb.coalesced) {
          this.ui.toast(
            "success",
            "A heartbeat was already running; showing it.",
          );
        } else if (hb.status === "running") {
          this.ui.toast("success", "Heartbeat started.");
        } else {
          // refused (paused / daily budget): recorded, never run
          this.ui.toast("error", `Wake refused: ${hb.error ?? hb.status}.`);
        }
        this.detail.set(hb);
        this.expanded.set(new Set());
        this.tick();
      },
      error: (err) => {
        this.waking.set(false);
        this.fail(err, "Wake failed.");
      },
    });
  }

  async cancel(d: HeartbeatDetail): Promise<void> {
    const ok = await this.ui.confirm(
      "Cancel this heartbeat? It stops before its next LLM or tool call; writes already made stay (revert them afterwards if needed).",
      {
        title: "Cancel heartbeat",
        confirmLabel: "Cancel heartbeat",
        danger: true,
      },
    );
    if (!ok) return;
    this.api.cancelHeartbeat(d.id).subscribe({
      next: (hb) => {
        this.detail.set(hb);
        this.ui.toast("success", "Cancel requested.");
      },
      error: (err) => this.fail(err, "Cancel refused."),
    });
  }

  async revert(d: HeartbeatDetail): Promise<void> {
    const n = d.journal.length;
    const ok = await this.ui.confirm(
      `Revert ${n} journalled write${n === 1 ? "" : "s"} made by this heartbeat? Each is restored to its recorded before-state, newest first, under your own credentials.`,
      {
        title: "Revert heartbeat writes",
        confirmLabel: "Revert",
        danger: true,
      },
    );
    if (!ok) return;
    this.api.revertHeartbeat(d.id).subscribe({
      next: (hb) => {
        this.detail.set(hb);
        this.ui.toast("success", `Reverted ${hb.reverted ?? n} write(s).`);
        this.tick();
      },
      error: (err) => this.fail(err, "Revert refused."),
    });
  }

  status(step: HeartbeatStep): string {
    if (step.kind === "llm")
      return step.tool_calls.length ? "tool calls" : "final";
    if (step.kind === "postcheck")
      return !step.ok
        ? "no evidence"
        : step.changed
          ? "corrected"
          : "confirmed";
    if (step.proposed) return "proposed";
    if (step.guardrail) return "refused";
    return step.ok ? "ok" : "error";
  }

  json(v: unknown): string {
    return JSON.stringify(v, null, 2);
  }

  private cls(step: HeartbeatStep): string {
    if (step.kind === "llm") return "run";
    if (step.kind === "postcheck") return step.ok ? "plan" : "bad";
    if (step.proposed) return "plan";
    return step.ok ? "ok" : "bad";
  }

  private refresh(id: string): void {
    this.api.heartbeat(id).subscribe({
      next: (hb) => this.detail.set(hb),
      error: () => this.ui.toast("error", "Could not load the heartbeat."),
    });
  }

  private fail(err: unknown, fallback: string): void {
    const status = (err as { status?: number })?.status;
    const p = problemsOf(err);
    this.ui.toast(
      "error",
      status === 403
        ? "Forbidden: this needs the agent's rbac.invoke role (revert: admin)."
        : status === 503
          ? "agent-hub has no LLM provider configured (Settings → AI assistant)."
          : p.length
            ? `${fallback} ${p[0]}`
            : fallback,
    );
  }
}
