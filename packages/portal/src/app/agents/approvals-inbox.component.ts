import { CommonModule } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  OnDestroy,
  OnInit,
  Output,
  inject,
  signal,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import { AuthService } from "../auth/auth.service";
import {
  AgentHubApiService,
  Approval,
  problemsOf,
} from "../shared/agent-hub-api.service";
import { PagePoll, PollHealth } from "../shared/poll-health";
import { UiService } from "../shared/ui.service";
import { StaleChipComponent } from "../shared/ui/stale-chip.component";

const POLL_MS = 10_000;

/**
 * Approvals inbox (ADR-0010 phase 3): every workflow run parked at an
 * `approval` node, across workflows. A decision needs one of the node's roles
 * (or admin); the decider is recorded on the approval and the run moves on,
 * or ends `rejected`. Polls through PollHealth so a dead agent-hub is never
 * read as an empty inbox.
 */
@Component({
  selector: "app-approvals-inbox",
  standalone: true,
  imports: [CommonModule, FormsModule, StaleChipComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="inbox" [class.inbox--empty]="!items().length">
      <div class="inbox__head">
        <h3>Approvals inbox</h3>
        <span class="muted">{{ items().length }} pending</span>
        <pp-stale-chip [health]="health" />
        <button class="link" (click)="showDecided.set(!showDecided())">
          {{ showDecided() ? "hide decided" : "show decided" }}
        </button>
      </div>

      @if (loading()) {
        <p class="muted">Loading approvals…</p>
      } @else if (!items().length && !showDecided()) {
        <p class="muted">Nothing waits for a human right now.</p>
      }

      @for (a of visible(); track a.id) {
        <div class="ap" [class.ap--decided]="a.status !== 'pending'">
          <div class="ap__row">
            <span class="mono strong">{{ a.workflow }}</span>
            <span class="muted">node</span>
            <span class="mono">{{ a.node_id }}</span>
            <span [class]="'pill pill--' + a.status">{{ a.status }}</span>
            <span class="muted grow">
              requested {{ a.requested_at | date: "short" }}
              @if (a.expires_at && a.status === "pending") {
                · expires {{ a.expires_at | date: "short" }}
              }
              @if (a.decided_by) {
                · {{ a.status }} by {{ a.decided_by }}
                {{ a.decided_at | date: "short" }}
              }
            </span>
            <span class="muted">roles: {{ a.roles.join(", ") }}</span>
            <button class="link" (click)="toggle(a.id)">
              {{ open() === a.id ? "hide context" : "context" }}
            </button>
          </div>

          @if (open() === a.id) {
            <div class="ap__ctx">
              @if (a.context.plans?.length) {
                <div class="muted small">
                  {{ a.context.plans!.length }} plan artifact(s):
                  <span class="mono">{{ a.context.plans!.join(", ") }}</span>
                </div>
              }
              <details open>
                <summary class="tsub">upstream results</summary>
                <pre class="pre">{{ json(a.context.results ?? {}) }}</pre>
              </details>
              <details>
                <summary class="tsub">run inputs</summary>
                <pre class="pre">{{ json(a.context.inputs ?? {}) }}</pre>
              </details>
              @if (a.note) {
                <div class="small">
                  <span class="muted">note:</span> {{ a.note }}
                </div>
              }
            </div>
          }

          @if (a.status === "pending") {
            <div class="ap__act">
              <input
                class="note"
                [(ngModel)]="notes[a.id]"
                placeholder="Note (recorded with the decision)"
                [disabled]="!canDecide(a) || busy() === a.id"
              />
              <button
                class="btn btn--primary"
                [disabled]="!canDecide(a) || busy() === a.id"
                [title]="
                  canDecide(a)
                    ? ''
                    : 'Needs one of: ' + a.roles.join(', ') + ' (or admin)'
                "
                (click)="decide(a, 'approved')"
              >
                Approve
              </button>
              <button
                class="btn btn--danger"
                [disabled]="!canDecide(a) || busy() === a.id"
                (click)="decide(a, 'rejected')"
              >
                Reject
              </button>
            </div>
          }
        </div>
      }
    </section>
  `,
  styles: [
    `
      .inbox {
        border: 1px solid var(--pp-warn-border, #f59e0b);
        border-radius: 10px;
        padding: 10px 14px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: var(--pp-warn-bg, rgba(245, 158, 11, 0.06));
      }
      .inbox--empty {
        border-color: var(--pp-border, #e5e7eb);
        background: transparent;
      }
      .inbox__head {
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
      .grow {
        flex: 1;
      }
      .link {
        border: 0;
        background: none;
        color: var(--brand, #5333ed);
        cursor: pointer;
        padding: 0;
        font: inherit;
      }
      .ap {
        border: 1px solid var(--pp-border, #e5e7eb);
        border-radius: 8px;
        padding: 8px 10px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        background: var(--pp-surface, transparent);
        font-size: 13px;
      }
      .ap--decided {
        opacity: 0.7;
      }
      .ap__row {
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
      }
      .ap__ctx {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .ap__act {
        display: flex;
        gap: 8px;
        align-items: center;
      }
      .note {
        flex: 1;
        font: inherit;
        padding: 5px 8px;
        border: 1px solid var(--pp-border, #e5e7eb);
        border-radius: 6px;
        background: var(--pp-input-bg, transparent);
        color: inherit;
      }
      .pre {
        margin: 2px 0;
        padding: 6px 8px;
        border-radius: 6px;
        background: var(--pp-surface-soft, rgba(127, 127, 127, 0.08));
        font-size: 12px;
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 240px;
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
      .pill--pending {
        border-color: #d97706;
        color: #b45309;
      }
      .pill--approved {
        border-color: #16a34a;
        color: #15803d;
      }
      .pill--rejected,
      .pill--expired {
        border-color: #dc2626;
        color: #b91c1c;
      }
      .pill--cancelled {
        opacity: 0.6;
      }
    `,
  ],
})
export class ApprovalsInboxComponent implements OnInit, OnDestroy {
  private readonly api = inject(AgentHubApiService);
  private readonly ui = inject(UiService);
  private readonly auth = inject(AuthService);

  /** Emits the pending count on every poll (for a tab badge). */
  @Output() readonly pending = new EventEmitter<number>();
  @Input() isAdmin = false;

  readonly health = new PollHealth();
  readonly items = signal<Approval[]>([]);
  readonly decided = signal<Approval[]>([]);
  readonly loading = signal(true);
  readonly showDecided = signal(false);
  readonly open = signal<string | null>(null);
  readonly busy = signal<string | null>(null);
  notes: Record<string, string> = {};

  readonly visible = () =>
    this.showDecided() ? [...this.items(), ...this.decided()] : this.items();

  private readonly poll = new PagePoll(POLL_MS, () => this.tick());

  ngOnInit(): void {
    this.poll.start();
  }

  ngOnDestroy(): void {
    this.poll.stop();
    this.health.destroy();
  }

  private tick(): void {
    this.api.approvals("pending").subscribe({
      next: (rows) => {
        this.items.set(rows);
        this.loading.set(false);
        this.health.markOk();
        this.pending.emit(rows.length);
      },
      error: () => {
        this.loading.set(false);
        this.health.markError();
      },
    });
    if (this.showDecided()) {
      this.api.approvals("all", undefined, 30).subscribe({
        next: (rows) =>
          this.decided.set(rows.filter((a) => a.status !== "pending")),
        error: () => {
          /* reported by the pending poll */
        },
      });
    }
  }

  canDecide(a: Approval): boolean {
    if (this.isAdmin) return true;
    const mine = new Set(this.auth.user()?.roles ?? []);
    return a.roles.some((r) => mine.has(r));
  }

  toggle(id: string): void {
    this.open.set(this.open() === id ? null : id);
  }

  async decide(a: Approval, decision: "approved" | "rejected"): Promise<void> {
    const ok = await this.ui.confirm(
      decision === "approved"
        ? `Approve '${a.node_id}' of ${a.workflow}? The run continues; a full-mode step after it will execute journalled writes.`
        : `Reject '${a.node_id}' of ${a.workflow}? Unless the workflow routes rejections, the run ends rejected.`,
      {
        title: decision === "approved" ? "Approve" : "Reject",
        confirmLabel: decision === "approved" ? "Approve" : "Reject",
        danger: decision === "rejected",
      },
    );
    if (!ok) return;
    this.busy.set(a.id);
    this.api.decide(a.id, decision, (this.notes[a.id] ?? "").trim()).subscribe({
      next: () => {
        this.busy.set(null);
        this.ui.toast("success", `${a.workflow} · ${a.node_id}: ${decision}.`);
        delete this.notes[a.id];
        this.tick();
      },
      error: (err) => {
        this.busy.set(null);
        const status = (err as { status?: number })?.status;
        const p = problemsOf(err);
        this.ui.toast(
          "error",
          status === 403
            ? "Forbidden: this approval needs one of its roles (or admin)."
            : status === 409
              ? "Already decided (or the run is no longer active)."
              : p[0] || "Decision failed.",
        );
        this.tick();
      },
    });
  }

  json(v: unknown): string {
    return JSON.stringify(v, null, 2);
  }
}
