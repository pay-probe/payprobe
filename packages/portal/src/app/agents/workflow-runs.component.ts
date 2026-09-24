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
  NodeState,
  RunDetail,
  RunSummary,
  WorkflowSpec,
  problemsOf,
} from "../shared/agent-hub-api.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { UiService } from "../shared/ui.service";
import { StaleChipComponent } from "../shared/ui/stale-chip.component";

const POLL_MS = 5_000;

/**
 * Runs of one workflow (ADR-0010 phase 3): start a run with inputs, watch it
 * move through its nodes (the persisted `node_states` are the source of
 * truth), cancel it, and follow the ids it leaves behind (heartbeats, plan
 * artifacts, approvals). Approvals themselves are decided in the inbox above
 * the page; this view only points at them.
 */
@Component({
  selector: "app-workflow-runs",
  standalone: true,
  imports: [CommonModule, FormsModule, StaleChipComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="wr">
      <div class="wr__head">
        <h3>Runs</h3>
        <span class="muted">{{ rows().length }} recorded</span>
        <pp-stale-chip [health]="health" />
      </div>

      <!-- ---------------------------------------------- start -->
      <div class="start">
        <label class="f">
          <span
            >Inputs (JSON){{
              inputKeys().length ? ": " + inputKeys().join(", ") : ""
            }}</span
          >
          <textarea
            class="mono"
            rows="3"
            [(ngModel)]="inputsText"
            [disabled]="!isAdmin || starting()"
            spellcheck="false"
          ></textarea>
        </label>
        <button
          class="btn btn--primary"
          [disabled]="!isAdmin || starting()"
          [title]="
            isAdmin
              ? 'Start a run of the active version'
              : 'Needs an admin role'
          "
          (click)="start()"
        >
          {{ starting() ? "Starting…" : "Start run" }}
        </button>
      </div>

      <!-- ---------------------------------------------- list -->
      @if (loading()) {
        <p class="muted">Loading runs…</p>
      } @else if (!rows().length) {
        <p class="muted">No runs yet for this workflow.</p>
      } @else {
        <table class="tbl">
          <thead>
            <tr>
              <th>started</th>
              <th>status</th>
              <th>v</th>
              <th>by</th>
              <th title="nodes done / nodes">progress</th>
              <th>finished</th>
            </tr>
          </thead>
          <tbody>
            @for (r of rows(); track r.id) {
              <tr
                [class.selected]="r.id === selectedId()"
                (click)="select(r.id)"
              >
                <td class="muted">{{ r.started_at | date: "short" }}</td>
                <td>
                  <span [class]="'pill pill--' + r.status">{{ r.status }}</span>
                  @if (r.cancel_requested && r.status !== "cancelled") {
                    <span class="pill pill--cancelled">cancel requested</span>
                  }
                </td>
                <td class="mono">{{ r.version }}</td>
                <td class="muted">{{ r.invoked_by || "—" }}</td>
                <td class="mono">{{ r.n_done }}/{{ r.n_nodes }}</td>
                <td class="muted">
                  {{ r.finished_at ? (r.finished_at | date: "short") : "—" }}
                </td>
              </tr>
            }
          </tbody>
        </table>
      }

      <!-- ---------------------------------------------- detail -->
      @if (detail(); as d) {
        <div class="det">
          <div class="det__head">
            <div>
              <span [class]="'pill pill--' + d.status">{{ d.status }}</span>
              <span class="mono muted det__id" [title]="d.id">{{
                d.id | slice: 0 : 12
              }}</span>
              <span class="muted">
                · v{{ d.version }} · spec {{ d.spec_sha256 | slice: 0 : 12 }} ·
                by {{ d.invoked_by || "?" }} · started
                {{ d.started_at | date: "short" }}
                @if (d.finished_at) {
                  · finished {{ d.finished_at | date: "short" }}
                }
              </span>
            </div>
            <div class="actions">
              @if (d.status === "running" || d.status === "waiting") {
                <button
                  class="btn btn--danger"
                  [disabled]="!isAdmin || d.cancel_requested"
                  (click)="cancel(d)"
                >
                  {{ d.cancel_requested ? "Cancel requested" : "Cancel run" }}
                </button>
              }
              <button class="btn" (click)="detail.set(null)">Close</button>
            </div>
          </div>

          @if (d.error) {
            <div class="det__error">{{ d.error }}</div>
          }
          @if (d.status === "waiting") {
            <div class="det__wait">
              Waiting for a human decision: see the approvals inbox above.
            </div>
          }

          <table class="tbl nodes">
            <thead>
              <tr>
                <th>node</th>
                <th>status</th>
                <th>agent · mode</th>
                <th>refs</th>
                <th>timing</th>
                <th>error</th>
              </tr>
            </thead>
            <tbody>
              @for (n of nodeRows(); track n.id) {
                <tr>
                  <td class="mono strong">{{ n.id }}</td>
                  <td>
                    <span [class]="'pill pill--n-' + n.st.status">{{
                      n.st.status
                    }}</span>
                  </td>
                  <td class="muted">
                    @if (n.st.agent) {
                      <span class="mono">{{ n.st.agent }}</span>
                      @if (n.st.mode) {
                        <span class="pill pill--mode">{{ n.st.mode }}</span>
                      }
                    }
                  </td>
                  <td class="mono small">
                    @if (n.st.heartbeat_id) {
                      <span title="heartbeat"
                        >hb {{ n.st.heartbeat_id | slice: 0 : 8 }}</span
                      >
                    }
                    @if (n.st.plan_id) {
                      <span title="plan artifact"
                        >· plan {{ n.st.plan_id | slice: 0 : 8 }}</span
                      >
                    }
                    @if (n.st.approval_id) {
                      <span title="approval"
                        >· ap {{ n.st.approval_id | slice: 0 : 8 }}</span
                      >
                    }
                    @if (n.st.journal?.length) {
                      <span title="journalled writes"
                        >· {{ n.st.journal!.length }} write(s)</span
                      >
                    }
                  </td>
                  <td class="muted small">
                    @if (n.st.started_at) {
                      {{ n.st.started_at | date: "mediumTime" }}
                      @if (n.st.finished_at) {
                        → {{ n.st.finished_at | date: "mediumTime" }}
                      }
                    }
                  </td>
                  <td class="small err">{{ n.st.error || "" }}</td>
                </tr>
              }
            </tbody>
          </table>

          <details>
            <summary class="tsub">inputs</summary>
            <pre class="pre">{{ json(d.inputs) }}</pre>
          </details>
          <details [open]="d.status === 'done'">
            <summary class="tsub">results by node</summary>
            <pre class="pre">{{ json(d.results) }}</pre>
          </details>
        </div>
      }
    </section>
  `,
  styles: [
    `
      .wr {
        display: flex;
        flex-direction: column;
        gap: 10px;
        border-top: 1px solid var(--pp-border, #e5e7eb);
        padding-top: 12px;
      }
      .wr__head {
        display: flex;
        gap: 10px;
        align-items: center;
      }
      h3 {
        margin: 0;
        font-size: 15px;
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
      .strong {
        font-weight: 600;
      }
      .start {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 8px;
        align-items: end;
      }
      .f {
        display: flex;
        flex-direction: column;
        gap: 4px;
        font-size: 13px;
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
      .tbl {
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
      }
      .tbl th,
      .tbl td {
        text-align: left;
        padding: 5px 8px;
        border-bottom: 1px solid var(--pp-border, #e5e7eb);
        vertical-align: top;
      }
      .tbl tbody tr {
        cursor: pointer;
      }
      .nodes tbody tr {
        cursor: default;
      }
      .tbl tr.selected td {
        background: var(--pp-row-selected, rgba(83, 51, 237, 0.06));
      }
      .det {
        border: 1px solid var(--pp-border, #e5e7eb);
        border-radius: 10px;
        padding: 12px 14px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        font-size: 13px;
      }
      .det__head {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: flex-start;
        flex-wrap: wrap;
      }
      .det__id {
        margin-left: 6px;
      }
      .actions {
        display: flex;
        gap: 8px;
      }
      .det__error {
        border: 1px solid #dc2626;
        border-radius: 8px;
        padding: 6px 10px;
      }
      .det__wait {
        border: 1px solid #d97706;
        border-radius: 8px;
        padding: 6px 10px;
      }
      .err {
        color: #b91c1c;
      }
      .pre {
        margin: 2px 0;
        padding: 6px 8px;
        border-radius: 6px;
        background: var(--pp-surface-soft, rgba(127, 127, 127, 0.08));
        font-size: 12px;
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 280px;
        overflow: auto;
      }
      .tsub {
        font-size: 11.5px;
        color: var(--pp-text-muted, #6b7280);
        text-transform: uppercase;
        cursor: pointer;
      }
      .pill {
        display: inline-block;
        padding: 1px 8px;
        border-radius: 999px;
        font-size: 11.5px;
        border: 1px solid var(--pp-border, #e5e7eb);
        text-transform: lowercase;
      }
      .pill--done,
      .pill--n-done {
        border-color: #16a34a;
        color: #15803d;
      }
      .pill--running,
      .pill--n-running {
        border-color: #4f7cf0;
        color: #4f7cf0;
      }
      .pill--waiting,
      .pill--n-waiting,
      .pill--n-deferred {
        border-color: #d97706;
        color: #b45309;
      }
      .pill--failed,
      .pill--rejected,
      .pill--n-failed {
        border-color: #dc2626;
        color: #b91c1c;
      }
      .pill--cancelled,
      .pill--n-cancelled,
      .pill--n-skipped,
      .pill--n-pending {
        opacity: 0.6;
      }
      .pill--mode {
        border-color: var(--brand, #5333ed);
        color: var(--brand, #5333ed);
        margin-left: 4px;
      }
    `,
  ],
})
export class WorkflowRunsComponent implements OnInit, OnChanges, OnDestroy {
  private readonly api = inject(AgentHubApiService);
  private readonly ui = inject(UiService);

  @Input({ required: true }) workflow!: string;
  /** The active spec, for the declared input names and node order. */
  @Input() spec: WorkflowSpec | null = null;
  @Input() isAdmin = false;

  readonly health = new PollHealth();
  readonly rows = signal<RunSummary[]>([]);
  readonly loading = signal(true);
  readonly detail = signal<RunDetail | null>(null);
  readonly starting = signal(false);
  inputsText = "{}";

  readonly selectedId = computed(() => this.detail()?.id ?? null);
  readonly inputKeys = computed(() => Object.keys(this.spec?.inputs ?? {}));
  /** Nodes in spec order (falls back to the run's own keys). */
  readonly nodeRows = computed<{ id: string; st: NodeState }[]>(() => {
    const d = this.detail();
    if (!d) return [];
    const order = this.spec?.nodes.map((n) => n.id) ?? [];
    const ids = [
      ...order,
      ...Object.keys(d.node_states).filter((k) => !order.includes(k)),
    ];
    return ids
      .filter((id) => d.node_states[id])
      .map((id) => ({ id, st: d.node_states[id] }));
  });

  private readonly poll = new PagePoll(POLL_MS, () => this.tick());

  ngOnInit(): void {
    this.resetInputs();
    this.poll.start();
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes["spec"]) this.resetInputs();
    if (changes["workflow"] && !changes["workflow"].firstChange) {
      this.rows.set([]);
      this.loading.set(true);
      this.detail.set(null);
      this.tick();
    }
  }

  ngOnDestroy(): void {
    this.poll.stop();
    this.health.destroy();
  }

  private resetInputs(): void {
    const keys = Object.keys(this.spec?.inputs ?? {});
    const draft: Record<string, string> = {};
    for (const k of keys) draft[k] = "";
    this.inputsText = JSON.stringify(draft, null, 2);
  }

  private tick(): void {
    this.api.runs(this.workflow).subscribe({
      next: (rows) => {
        this.rows.set(rows);
        this.loading.set(false);
        this.health.markOk();
        const open = this.detail();
        if (open) {
          const listed = rows.find((r) => r.id === open.id);
          const active = open.status === "running" || open.status === "waiting";
          if (active || (listed && listed.status !== open.status))
            this.refresh(open.id);
        }
      },
      error: () => {
        this.loading.set(false);
        this.health.markError();
      },
    });
  }

  select(id: string): void {
    if (this.selectedId() !== id) this.refresh(id);
  }

  start(): void {
    let inputs: Record<string, unknown>;
    try {
      const parsed = JSON.parse(this.inputsText || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("inputs must be a JSON object");
      }
      inputs = parsed;
    } catch (e) {
      this.ui.toast(
        "error",
        `Inputs are not valid JSON: ${(e as Error).message}`,
      );
      return;
    }
    this.starting.set(true);
    this.api.runWorkflow(this.workflow, inputs).subscribe({
      next: (run) => {
        this.starting.set(false);
        this.detail.set(run);
        this.ui.toast(
          "success",
          run.status === "waiting"
            ? "Run started and is already waiting for an approval."
            : `Run started (${run.status}).`,
        );
        this.tick();
      },
      error: (err) => {
        this.starting.set(false);
        this.fail(err, "Could not start the run.");
      },
    });
  }

  async cancel(d: RunDetail): Promise<void> {
    const ok = await this.ui.confirm(
      "Cancel this run? Running agent tasks are asked to stop and pending approvals are closed.",
      { title: "Cancel run", confirmLabel: "Cancel run", danger: true },
    );
    if (!ok) return;
    this.api.cancelRun(d.id).subscribe({
      next: (run) => {
        this.detail.set(run);
        this.ui.toast("success", "Run cancelled.");
        this.tick();
      },
      error: (err) => this.fail(err, "Cancel refused."),
    });
  }

  json(v: unknown): string {
    return JSON.stringify(v, null, 2);
  }

  private refresh(id: string): void {
    this.api.run(id).subscribe({
      next: (run) => this.detail.set(run),
      error: () => this.ui.toast("error", "Could not load the run."),
    });
  }

  private fail(err: unknown, fallback: string): void {
    const status = (err as { status?: number })?.status;
    const p = problemsOf(err);
    this.ui.toast(
      "error",
      status === 403
        ? "Forbidden: starting or cancelling runs needs an admin role."
        : status === 404
          ? "No runnable (active) version of this workflow."
          : p.length
            ? `${fallback} ${p.join("; ")}`
            : fallback,
    );
  }
}
