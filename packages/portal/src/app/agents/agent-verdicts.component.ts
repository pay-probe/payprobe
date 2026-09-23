import { CommonModule } from "@angular/common";
import {
  ChangeDetectionStrategy,
  Component,
  Input,
  OnChanges,
  SimpleChanges,
  inject,
  signal,
} from "@angular/core";
import { RouterLink } from "@angular/router";
import { forkJoin, of } from "rxjs";
import { catchError, map, switchMap } from "rxjs/operators";

import {
  AgentHubApiService,
  HeartbeatDetail,
} from "../shared/agent-hub-api.service";

/** One agent verdict as the run report shows it. */
interface Verdict {
  id: string;
  agent: string;
  wake: string;
  status: string;
  at: string;
  /** failure-triage style: one object */
  category?: string;
  rootCause?: string;
  nextStep?: string;
  regression?: unknown;
  /** observer style: a findings list */
  findings?: { severity?: string; subject?: string; headline?: string }[];
  /** neither: the first lines of the answer */
  text?: string;
  error?: string | null;
}

/** The JSON an agent's answer carries: bare, fenced, or inside a report. */
function extractJson(text: string | null): unknown {
  const s = (text ?? "").trim();
  if (!s) return null;
  const candidates: string[] = [s];
  const fence = /```(?:json|JSON)?\s*\n([\s\S]*?)```/g;
  let m: RegExpExecArray | null;
  while ((m = fence.exec(s)) !== null) candidates.push(m[1].trim());
  for (const [open, close] of [
    ["[", "]"],
    ["{", "}"],
  ]) {
    const i = s.indexOf(open);
    const j = s.lastIndexOf(close);
    if (i >= 0 && i < j) candidates.push(s.slice(i, j + 1));
  }
  for (const c of candidates) {
    if (!c || !"[{".includes(c[0])) continue;
    try {
      const v = JSON.parse(c);
      if (v && typeof v === "object") return v;
    } catch {
      /* try the next candidate */
    }
  }
  return null;
}

function toVerdict(hb: HeartbeatDetail): Verdict {
  const base: Verdict = {
    id: hb.id,
    agent: hb.agent,
    wake: hb.wake,
    status: hb.status,
    at: hb.finished_at ?? hb.started_at,
    error: hb.error,
  };
  const data = extractJson(hb.result);
  if (Array.isArray(data)) {
    return {
      ...base,
      findings: data.filter((f) => f && typeof f === "object"),
    };
  }
  if (data && typeof data === "object") {
    const d = data as Record<string, unknown>;
    if (Array.isArray(d["findings"])) {
      return { ...base, findings: d["findings"] as Verdict["findings"] };
    }
    return {
      ...base,
      category: typeof d["category"] === "string" ? d["category"] : undefined,
      rootCause:
        typeof d["root_cause"] === "string" ? d["root_cause"] : undefined,
      nextStep: typeof d["next_step"] === "string" ? d["next_step"] : undefined,
      regression: d["regression"],
    };
  }
  return { ...base, text: (hb.result ?? "").slice(0, 400) };
}

/**
 * Agent verdicts attached to one subject (a run: `run:<id>`), for the run
 * report (ADR-0010). Reads finished heartbeats whose `subject` matches and
 * renders what each agent concluded: failure-triage's category, root cause and
 * next step, or observer-style findings. Advisory by construction: the report
 * attaches this text, it never consults it, and gates stay deterministic.
 * Renders nothing when agent-hub is not deployed or has no verdict yet.
 */
@Component({
  selector: "app-agent-verdicts",
  standalone: true,
  imports: [CommonModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (verdicts().length) {
      <section class="av">
        <div class="av__head">
          <span class="av__badge">agent verdict — advisory</span>
          <span class="muted"
            >what the agents concluded about this run; gates stay
            deterministic</span
          >
          <a class="av__link" routerLink="/agents">open Agents →</a>
        </div>
        @for (v of verdicts(); track v.id) {
          <div class="av__item" [class.av__item--failed]="v.status !== 'done'">
            <div class="av__row">
              <span class="mono strong">{{ v.agent }}</span>
              <span class="muted small"
                >{{ v.wake }} wake · {{ v.at | date: "short" }} · heartbeat
                {{ v.id.slice(0, 8) }}</span
              >
              @if (v.status !== "done") {
                <span class="pill pill--bad">{{ v.status }}</span>
              }
              @if (v.category) {
                <span class="pill">{{ v.category }}</span>
              }
              @if (v.regression === true) {
                <span
                  class="pill pill--warn"
                  title="the agent judged this a regression"
                  >regression</span
                >
              }
            </div>
            @if (v.error) {
              <p class="av__err">{{ v.error }}</p>
            }
            @if (v.rootCause) {
              <p class="av__text">
                <strong>Root cause.</strong> {{ v.rootCause }}
              </p>
            }
            @if (v.nextStep) {
              <p class="av__text">
                <strong>Next step.</strong> {{ v.nextStep }}
              </p>
            }
            @if (v.findings?.length) {
              <ul class="av__findings">
                @for (f of v.findings; track $index) {
                  <li>
                    <span [class]="'pill pill--' + (f.severity || 'info')">{{
                      f.severity || "info"
                    }}</span>
                    <span class="mono small">{{ f.subject }}</span>
                    {{ f.headline }}
                  </li>
                }
              </ul>
            }
            @if (v.text) {
              <p class="av__text muted">{{ v.text }}</p>
            }
          </div>
        }
      </section>
    }
  `,
  styles: [
    `
      .av {
        border: 1px dashed var(--pp-border, #cdd4e0);
        border-radius: 10px;
        padding: 10px 14px;
        margin: 8px 0;
        display: flex;
        flex-direction: column;
        gap: 8px;
        font-size: 13px;
      }
      .av__head {
        display: flex;
        gap: 10px;
        align-items: center;
        flex-wrap: wrap;
      }
      .av__badge {
        border: 1px solid var(--brand, #5333ed);
        color: var(--brand, #5333ed);
        border-radius: 999px;
        padding: 1px 8px;
        font-size: 11.5px;
      }
      .av__link {
        margin-left: auto;
        color: var(--brand, #5333ed);
        text-decoration: none;
        font-size: 12px;
      }
      .av__item {
        border-top: 1px solid var(--pp-border, #e5e7eb);
        padding-top: 6px;
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .av__item--failed {
        opacity: 0.8;
      }
      .av__row {
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
      }
      .av__text {
        margin: 0;
      }
      .av__err {
        margin: 0;
        color: #b91c1c;
      }
      .av__findings {
        margin: 0;
        padding-left: 18px;
      }
      .av__findings li {
        margin: 2px 0;
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
      .pill {
        display: inline-block;
        padding: 0 7px;
        border-radius: 999px;
        border: 1px solid var(--pp-border, #e5e7eb);
        font-size: 11px;
        text-transform: lowercase;
      }
      .pill--critical,
      .pill--bad,
      .pill--error {
        border-color: #dc2626;
        color: #b91c1c;
      }
      .pill--warn,
      .pill--warning {
        border-color: #d97706;
        color: #b45309;
      }
    `,
  ],
})
export class AgentVerdictsComponent implements OnChanges {
  private readonly api = inject(AgentHubApiService);

  /** e.g. `run:<run id>` */
  @Input({ required: true }) subject!: string;

  readonly verdicts = signal<Verdict[]>([]);

  ngOnChanges(changes: SimpleChanges): void {
    if (changes["subject"]) this.load();
  }

  private load(): void {
    const subject = this.subject;
    if (!subject) {
      this.verdicts.set([]);
      return;
    }
    this.api
      .heartbeats(undefined, 20, subject)
      .pipe(
        map((rows) => rows.filter((r) => r.status !== "running")),
        switchMap((rows) =>
          rows.length
            ? forkJoin(rows.map((r) => this.api.heartbeat(r.id)))
            : of([] as HeartbeatDetail[]),
        ),
        map((details) => details.map(toVerdict)),
        // agent-hub is an optional deployment: no panel is the right answer
        catchError(() => of([] as Verdict[])),
      )
      .subscribe((v) => this.verdicts.set(v));
  }
}
