import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";

import { ScenarioApiService } from "../constructor/scenario-api.service";
import { Pack, PackSummary } from "../constructor/scenario.models";
import { Certification, RunApiService } from "../run-monitor/run-api.service";
import { UiService } from "../shared/ui.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

interface PackState {
  detail?: Pack;
  expanded?: boolean;
  projectId?: string;
  runId?: string;
  running?: boolean;
  cert?: Certification;
}

const TERMINAL = new Set([
  "passed",
  "failed",
  "blocked",
  "partial",
  "error",
  "cancelled",
]);

/** Test-case packs — curated certification suites. Install a pack (imports its
 *  scenarios into a project), run it, and view the certification report. */
@Component({
  selector: "app-packs",
  standalone: true,
  imports: [PageHeaderComponent],
  template: `
    <div class="pk">
      <pp-page-header>
        <div subtitle>
          <span class="muted"
            >Curated certification suites — install, run, certify</span
          >
        </div>
      </pp-page-header>
      <div class="pk__body">
        @if (loading()) {
          <p class="muted">Loading…</p>
        }
        @for (p of packs(); track p.id) {
          <article class="card">
            <div class="card__h">
              <div>
                <span class="scheme">{{ p.scheme }}</span>
                <b>{{ p.label }}</b>
                <span class="muted">
                  · {{ p.cases }} cases · v{{ p.version }}</span
                >
              </div>
              <button class="link" (click)="toggle(p)">
                {{ st(p).expanded ? "Hide cases" : "Show cases" }}
              </button>
            </div>
            <p class="desc">{{ p.description }}</p>

            @if (st(p).expanded && st(p).detail) {
              <ul class="cases">
                @for (c of st(p).detail!.cases; track c.id) {
                  <li>
                    <code>{{ c.id }}</code> — {{ c.requirement }}
                  </li>
                }
              </ul>
            }

            <div class="actions">
              <button
                class="btn"
                (click)="install(p)"
                [disabled]="st(p).running"
              >
                Install
              </button>
              <button
                class="btn btn--primary"
                (click)="runCertify(p)"
                [disabled]="!st(p).projectId || st(p).running"
              >
                {{ st(p).running ? "Running…" : "Run & certify" }}
              </button>
              @if (st(p).projectId) {
                <span class="ok">installed → {{ st(p).projectId }}</span>
              }
            </div>

            @if (st(p).cert; as cert) {
              <div class="cert">
                <span class="badge" [class.on]="cert.certified">
                  {{ cert.certified ? "✓ CERTIFIED" : "✗ NOT CERTIFIED" }}
                </span>
                <span class="muted"
                  >{{ cert.compliance_pct }}% ({{ cert.passed }}/{{
                    cert.total
                  }})</span
                >
                <button class="link" (click)="openCert(p)">
                  Open report ↗
                </button>
                <ul class="results">
                  @for (c of cert.cases; track c.id) {
                    <li [class.p]="c.passed" [class.f]="!c.passed">
                      {{ c.passed ? "✓" : c.status === "not_run" ? "·" : "✗" }}
                      {{ c.requirement }}
                      <span class="muted">({{ c.status }})</span>
                    </li>
                  }
                </ul>
              </div>
            }
          </article>
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
        background: var(--color-bg);
        color: var(--color-text);
      }
      .pk {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .pk__bar {
        padding: 12px 16px;
        border-bottom: 1px solid var(--color-border);
      }
      .pk__bar h1 {
        margin: 0;
        font-size: 16px;
      }
      .pk__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 14px;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
      }
      .card {
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 14px;
        background: var(--color-node, var(--color-bg));
      }
      .card__h {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
      }
      .scheme {
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        font-weight: 700;
        color: #fff;
        background: var(--color-primary, #58a6ff);
        padding: 2px 7px;
        border-radius: 999px;
        margin-right: 8px;
      }
      .desc {
        font-size: 13px;
        color: var(--color-text-muted, var(--color-text));
        margin: 6px 0 0;
      }
      .cases {
        margin: 10px 0 0;
        padding-left: 18px;
        font-size: 13px;
        line-height: 1.7;
      }
      .cases code,
      .results code {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 0.9em;
      }
      .actions {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-top: 12px;
        flex-wrap: wrap;
      }
      .ok {
        font-size: 12px;
        color: var(--color-success, #2ea043);
        font-family: var(--font-mono, ui-monospace, monospace);
      }
      .cert {
        margin-top: 12px;
        border-top: 1px solid var(--color-border);
        padding-top: 10px;
      }
      .badge {
        font-weight: 700;
        font-size: 12px;
        padding: 2px 10px;
        border-radius: 999px;
        border: 1px solid var(--color-danger, #f85149);
        color: var(--color-danger, #f85149);
      }
      .badge.on {
        border-color: var(--color-success, #2ea043);
        color: var(--color-success, #2ea043);
      }
      .results {
        margin: 8px 0 0;
        padding-left: 18px;
        font-size: 13px;
        line-height: 1.7;
        list-style: none;
      }
      .results li.p {
        color: var(--color-success, #2ea043);
      }
      .results li.f {
        color: var(--color-danger, #f85149);
      }
      .link {
        background: none;
        border: none;
        color: var(--color-primary, #58a6ff);
        cursor: pointer;
        font: inherit;
        font-size: 13px;
        padding: 0;
        text-decoration: none;
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
    `,
  ],
})
export class PacksComponent implements OnInit {
  private readonly api = inject(ScenarioApiService);
  private readonly runApi = inject(RunApiService);
  private readonly ui = inject(UiService);

  readonly packs = signal<PackSummary[]>([]);
  readonly loading = signal(true);
  private readonly state = signal<Record<string, PackState>>({});

  ngOnInit() {
    this.api.packs().subscribe({
      next: (p) => {
        this.packs.set(p);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  st(p: PackSummary): PackState {
    return this.state()[p.id] ?? {};
  }

  private patch(id: string, s: Partial<PackState>) {
    this.state.update((m) => ({ ...m, [id]: { ...m[id], ...s } }));
  }

  toggle(p: PackSummary) {
    const cur = this.st(p);
    if (cur.detail) {
      this.patch(p.id, { expanded: !cur.expanded });
      return;
    }
    this.api
      .pack(p.id)
      .subscribe((d) => this.patch(p.id, { detail: d, expanded: true }));
  }

  install(p: PackSummary) {
    this.api.installPack(p.id).subscribe({
      next: (r) => {
        this.patch(p.id, { projectId: r.project_id });
        this.ui.toast(
          "success",
          `Installed ${r.imported} scenarios → ${r.project_name}`,
        );
      },
      error: () =>
        this.ui.toast(
          "error",
          "Install failed — is the scenario service running?",
        ),
    });
  }

  runCertify(p: PackSummary) {
    const projectId = this.st(p).projectId;
    if (!projectId) return;
    this.patch(p.id, { running: true, cert: undefined });
    this.runApi
      .start({ project_id: projectId, label: `${p.label} certification` })
      .subscribe({
        next: (resp) => {
          this.patch(p.id, { runId: resp.run_id });
          this.poll(p, resp.run_id, 0);
        },
        error: () => {
          this.patch(p.id, { running: false });
          this.ui.toast("error", "Could not start run.");
        },
      });
  }

  private poll(p: PackSummary, runId: string, tries: number) {
    this.runApi.get(runId).subscribe({
      next: (d) => {
        if (TERMINAL.has(d.status) || tries > 40) {
          this.runApi.certification(runId, p.id).subscribe({
            next: (cert) => this.patch(p.id, { running: false, cert }),
            error: () => {
              this.patch(p.id, { running: false });
              this.ui.toast("error", "Certification failed.");
            },
          });
        } else {
          setTimeout(() => this.poll(p, runId, tries + 1), 1200);
        }
      },
      error: () => {
        this.patch(p.id, { running: false });
        this.ui.toast("error", "Run polling failed.");
      },
    });
  }

  /** Authenticated fetch → blob tab; a plain anchor carries no bearer token
   *  and would download the JWT gate's 401 JSON instead of the report. */
  openCert(p: PackSummary): void {
    const runId = this.st(p).runId;
    if (!runId) return;
    this.runApi
      .fetchBlob(this.runApi.certificationHtmlUrl(runId, p.id))
      .subscribe((b) => {
        const url = URL.createObjectURL(new Blob([b], { type: "text/html" }));
        window.open(url, "_blank");
        setTimeout(() => URL.revokeObjectURL(url), 60_000);
      });
  }
}
