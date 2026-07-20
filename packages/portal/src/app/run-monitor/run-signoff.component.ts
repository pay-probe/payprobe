import { CommonModule, DatePipe } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  inject,
  signal,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { ActivatedRoute, RouterLink } from "@angular/router";

import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { RunApiService } from "./run-api.service";
import { Signoff } from "./run.models";

/**
 * Go/No-Go sign-off view (ADR-0003). Verdict-first: a single GO / NO-GO banner
 * and the gates that produced it, then provenance + the approval trail. This is
 * the artifact a release is signed off against — detail is supporting evidence,
 * not the headline. Distinct from the improvement-mode run report.
 */
@Component({
  selector: "app-run-signoff",
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    DatePipe,
    RouterLink,
    PageHeaderComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="so">
      <pp-page-header>
        <span class="so__actions">
          @if (signoff(); as s) {
            <button class="btn" (click)="openDoc()">⤓ PDF / print</button>
          }
          <a class="btn btn--ghost" [routerLink]="['/reports', runId]">
            ← Run report
          </a>
        </span>
      </pp-page-header>

      @if (error()) {
        <p class="so__error">{{ error() }}</p>
      }

      <!-- No sign-off yet: offer to certify -->
      @if (!signoff() && !loading()) {
        <section class="card certify">
          <h2>Certify this run for production</h2>
          <p class="muted">
            Evaluate the gates, stamp provenance, and freeze an immutable
            sign-off. The regression gate compares against the last GO run for
            this pack + environment.
          </p>
          <div class="certify__row">
            <label
              >Pack
              <input
                [(ngModel)]="pack"
                placeholder="e.g. visa-base1"
                class="input"
            /></label>
            <label
              >Project (optional) <input [(ngModel)]="project" class="input"
            /></label>
            <button
              class="btn btn--primary"
              [disabled]="!pack || busy()"
              (click)="certify()"
            >
              {{ busy() ? "Certifying…" : "Certify" }}
            </button>
          </div>
        </section>
      }

      @if (signoff(); as s) {
        <!-- verdict banner -->
        <section
          class="verdict"
          [class.verdict--go]="s.verdict === 'GO'"
          [class.verdict--nogo]="s.verdict === 'NO_GO'"
        >
          <div class="verdict__badge">
            {{ s.verdict === "GO" ? "GO" : "NO-GO" }}
          </div>
          <div class="verdict__meta">
            <div>
              <strong>{{ s.pack || "—" }}</strong>
              <span class="muted"> · {{ s.environment || "—" }}</span>
            </div>
            <div class="muted mono">
              run {{ s.run_id.slice(0, 8) }} · certified
              {{ s.created_at | date: "medium" }} by {{ s.created_by }}
            </div>
          </div>
        </section>

        <!-- gates -->
        <section class="card">
          <h3>Gates</h3>
          <ul class="gates">
            @for (g of s.gate_result.gates; track g.id) {
              <li class="gate" [class.gate--bad]="!g.ok">
                <span class="gate__mark">{{ g.ok ? "✓" : "✗" }}</span>
                <span class="gate__id mono">{{ g.id }}</span>
                <span class="gate__detail muted">{{ g.detail }}</span>
              </li>
            }
          </ul>
        </section>

        <!-- provenance -->
        <section class="card">
          <h3>Provenance</h3>
          <dl class="prov">
            <dt>Content hash</dt>
            <dd class="mono">{{ s.content_hash }}</dd>
            <dt>Environment</dt>
            <dd>{{ s.provenance.environment || "—" }}</dd>
            <dt>Pack</dt>
            <dd>
              {{ s.provenance.pack?.id || "—" }}
              <span class="muted">{{ s.provenance.pack?.version }}</span>
            </dd>
            <dt>Build (SUT)</dt>
            <dd class="mono">
              {{ s.provenance.system_under_test?.build || "—" }}
            </dd>
            <dt>Baseline run</dt>
            <dd class="mono">{{ s.provenance.baseline_run_id || "none" }}</dd>
            <dt>Endpoints</dt>
            <dd>
              @if (s.provenance.endpoints.length) {
                @for (e of s.provenance.endpoints; track e.target) {
                  <span class="chip mono"
                    >{{ e.target }}: {{ e.host }}:{{ e.port }}</span
                  >
                }
              } @else {
                <span class="muted">none recorded</span>
              }
            </dd>
          </dl>
        </section>

        <!-- approval trail -->
        <section class="card">
          <h3>Approvals</h3>
          @if (s.approvals.length) {
            <ul class="trail">
              @for (a of s.approvals; track a.at) {
                <li>
                  <strong>{{ a.by }}</strong>
                  <span class="chip">{{ a.decision }}</span>
                  <span class="muted">{{ a.at | date: "short" }}</span>
                  @if (a.note) {
                    <div class="muted">{{ a.note }}</div>
                  }
                </li>
              }
            </ul>
          } @else {
            <p class="muted">No approvals recorded yet.</p>
          }
          <div class="approve">
            <input
              [(ngModel)]="note"
              class="input"
              placeholder="Optional note"
            />
            <button class="btn" [disabled]="busy()" (click)="approve()">
              Sign off
            </button>
          </div>
        </section>
      }
    </div>
  `,
  styles: [
    `
      .so {
        padding: 1.5rem;
        color: var(--color-text);
      }
      .so__error {
        color: var(--color-fail);
      }
      .so__actions {
        display: flex;
        gap: 0.5rem;
      }
      .muted {
        color: var(--color-muted);
      }
      .mono {
        font-family: ui-monospace, monospace;
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 1rem 1.2rem;
        margin-bottom: 1rem;
        background: var(--color-card);
      }
      .card h3 {
        margin: 0 0 0.6rem;
        font-size: 1rem;
      }
      .btn {
        padding: 0.5rem 0.9rem;
        border-radius: 8px;
        border: 1px solid var(--color-border);
        background: var(--color-card);
        color: var(--color-text);
        cursor: pointer;
        font-weight: 600;
        text-decoration: none;
      }
      .btn--primary {
        background: var(--color-primary);
        color: #fff;
        border-color: var(--color-primary);
      }
      .btn--ghost {
        background: none;
      }
      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .input {
        padding: 0.45rem 0.6rem;
        border-radius: 8px;
        border: 1px solid var(--color-border);
        background: var(--color-bg-soft);
        color: var(--color-text);
      }
      .certify__row {
        display: flex;
        gap: 0.8rem;
        align-items: flex-end;
        flex-wrap: wrap;
        margin-top: 0.8rem;
      }
      .certify__row label {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
        font-size: 0.82rem;
        color: var(--color-muted);
      }
      /* verdict banner */
      .verdict {
        display: flex;
        gap: 1rem;
        align-items: center;
        border-radius: 12px;
        padding: 1rem 1.3rem;
        margin-bottom: 1rem;
        border: 2px solid var(--color-border);
      }
      .verdict--go {
        border-color: var(--color-success);
        background: color-mix(in srgb, var(--color-success) 12%, transparent);
      }
      .verdict--nogo {
        border-color: var(--color-danger);
        background: color-mix(in srgb, var(--color-danger) 12%, transparent);
      }
      .verdict__badge {
        font-size: 1.6rem;
        font-weight: 800;
        letter-spacing: 0.04em;
        padding: 0.3rem 1rem;
        border-radius: 10px;
      }
      .verdict--go .verdict__badge {
        background: var(--color-success);
        color: #fff;
      }
      .verdict--nogo .verdict__badge {
        background: var(--color-danger);
        color: #fff;
      }
      /* gates */
      .gates,
      .trail {
        list-style: none;
        margin: 0;
        padding: 0;
        display: flex;
        flex-direction: column;
        gap: 0.4rem;
      }
      .gate {
        display: grid;
        grid-template-columns: 18px 140px 1fr;
        gap: 0.6rem;
        align-items: baseline;
        font-size: 0.88rem;
      }
      .gate__mark {
        color: var(--color-success);
        font-weight: 700;
      }
      .gate--bad .gate__mark {
        color: var(--color-fail);
      }
      .gate--bad .gate__id {
        color: var(--color-fail);
      }
      /* provenance */
      .prov {
        display: grid;
        grid-template-columns: 130px 1fr;
        gap: 0.4rem 1rem;
        margin: 0;
        font-size: 0.88rem;
      }
      .prov dt {
        color: var(--color-muted);
      }
      .prov dd {
        margin: 0;
        word-break: break-all;
      }
      .chip {
        display: inline-block;
        padding: 0.1rem 0.5rem;
        border-radius: 999px;
        border: 1px solid var(--color-border);
        font-size: 0.76rem;
        margin: 0 0.3rem 0.2rem 0;
      }
      .approve {
        display: flex;
        gap: 0.6rem;
        margin-top: 0.8rem;
      }
      .approve .input {
        flex: 1;
      }
    `,
  ],
})
export class RunSignoffComponent implements OnInit {
  protected readonly api = inject(RunApiService);
  private readonly route = inject(ActivatedRoute);

  runId = "";
  pack = "";
  project = "";
  note = "";

  readonly signoff = signal<Signoff | null>(null);
  readonly loading = signal(true);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.runId = this.route.snapshot.paramMap.get("id") ?? "";
    if (!this.runId) {
      this.error.set("No run id.");
      this.loading.set(false);
      return;
    }
    // Show the most recent existing sign-off for this run, if any.
    this.api.signoffs({ run_id: this.runId }).subscribe({
      next: (r) => {
        this.signoff.set(r.signoffs[0] ?? null);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  /** Open the printable sign-off doc via an authenticated fetch — a plain
   *  target="_blank" navigation carries no bearer token and lands on the
   *  orchestrator's 401 instead of the document. */
  openDoc(): void {
    const s = this.signoff();
    if (!s) return;
    this.api.fetchBlob(this.api.signoffHtmlUrl(s.id)).subscribe((b) => {
      const url = URL.createObjectURL(new Blob([b], { type: "text/html" }));
      window.open(url, "_blank");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    });
  }

  certify(): void {
    if (!this.pack) return;
    this.busy.set(true);
    this.error.set(null);
    this.api
      .certify(this.runId, {
        pack: this.pack,
        project: this.project || undefined,
      })
      .subscribe({
        next: (s) => {
          this.signoff.set(s);
          this.busy.set(false);
        },
        error: (e) => {
          this.error.set(e?.error?.detail ?? "Certification failed.");
          this.busy.set(false);
        },
      });
  }

  approve(): void {
    const s = this.signoff();
    if (!s) return;
    this.busy.set(true);
    this.api
      .approveSignoff(s.id, { decision: "approved", note: this.note })
      .subscribe({
        next: (updated) => {
          this.signoff.set(updated);
          this.note = "";
          this.busy.set(false);
        },
        error: () => this.busy.set(false),
      });
  }
}
