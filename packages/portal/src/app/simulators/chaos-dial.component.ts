import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  effect,
  inject,
  input,
  signal,
} from "@angular/core";

import {
  ChaosBlock,
  ChaosPhase,
  ChaosStatus,
  RunApiService,
} from "../run-monitor/run-api.service";
import { UiService } from "../shared/ui.service";
import { PagePoll } from "../shared/poll-health";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { BadgeComponent } from "../shared/ui/badge.component";

/** A one-click fault profile applied to the live simulator. */
interface Preset {
  key: string;
  label: string;
  hint: string;
  chaos: ChaosBlock;
}

/** A timed chaos sequence (outage / brownout / flapping). */
interface Storm {
  key: string;
  label: string;
  hint: string;
  phases: ChaosPhase[];
  repeat: number;
}

const PRESETS: Preset[] = [
  {
    key: "slow",
    label: "Slow host",
    hint: "50–400ms on 40% of replies",
    chaos: { latency_ms: { min: 50, max: 400, pct: 40 } },
  },
  {
    key: "lossy",
    label: "Lossy link",
    hint: "10% of replies dropped → client timeouts",
    chaos: { drop_pct: 10 },
  },
  {
    key: "garbled",
    label: "Garbled frames",
    hint: "8% malformed replies (parse errors)",
    chaos: { malformed_pct: 8, malformed_mode: "garbage" },
  },
  {
    key: "truncated",
    label: "Truncated",
    hint: "8% cut off mid-frame",
    chaos: { partial_pct: 8, partial_bytes: "half" },
  },
  {
    key: "brutal",
    label: "Everything",
    hint: "latency + drops + malformed + partial",
    chaos: {
      latency_ms: { min: 100, max: 600, pct: 50 },
      drop_pct: 8,
      malformed_pct: 5,
      malformed_mode: "flip_bits",
      partial_pct: 4,
    },
  },
];

const STORMS: Storm[] = [
  {
    key: "outage",
    label: "30s outage",
    hint: "total drop for 30s, then recover",
    repeat: 1,
    phases: [{ duration_s: 30, chaos: { drop_pct: 100 }, label: "outage" }],
  },
  {
    key: "brownout",
    label: "Brownout ramp",
    hint: "20s mild → 20s heavy latency, then clear",
    repeat: 1,
    phases: [
      {
        duration_s: 20,
        chaos: { latency_ms: { min: 100, max: 300, pct: 60 } },
        label: "brownout·mild",
      },
      {
        duration_s: 20,
        chaos: { latency_ms: { min: 400, max: 1200, pct: 90 } },
        label: "brownout·heavy",
      },
    ],
  },
  {
    key: "flap",
    label: "Flapping ×5",
    hint: "10s down / 10s up, five cycles",
    repeat: 5,
    phases: [
      { duration_s: 10, chaos: { drop_pct: 100 }, label: "down" },
      { duration_s: 10, chaos: {}, label: "up" },
    ],
  },
];

/**
 * The chaos dial — a resilience-testing control surface on a running
 * simulator. One-click fault presets (slow / lossy / garbled / truncated /
 * brutal) toggle the live ``chaos`` block via PUT /simulators/{id}/chaos, and
 * timed "storms" (outage, brownout ramp, flapping) run a phased sequence that
 * restores the baseline when it ends. Polls GET /chaos so the current profile
 * and storm progress stay live. No restart, no config edit — the switch stays
 * on the wire.
 */
@Component({
  selector: "app-chaos-dial",
  standalone: true,
  imports: [ButtonComponent, IconComponent, BadgeComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="panel chaos">
      <div class="panel__head">
        <h3><pp-icon name="zap" [size]="15" /> Chaos dial</h3>
        @if (active()) {
          <pp-badge tone="warning">faults on</pp-badge>
        } @else {
          <span class="muted sm">off</span>
        }
        @if (status()?.faults) {
          <span class="muted sm faults">{{ status()!.faults }} injected</span>
        }
      </div>

      <p class="muted sm intro">
        Make this simulator misbehave to exercise the client's timeout, retry
        and parse-error paths. Applies live — no restart.
      </p>

      @if (storm(); as st) {
        <div class="storm">
          <span class="storm__pulse"></span>
          <span class="storm__txt">
            Storm running — <b>{{ st.phase }}</b> (cycle {{ st.cycle }}/{{
              st.repeat
            }})
          </span>
          <span class="storm__t mono">{{ st.phase_remaining_s }}s</span>
          <button class="storm__stop" (click)="stopStorm()">Stop</button>
        </div>
      }

      <div class="grp">
        <div class="grp__hd">Fault presets</div>
        <div class="chips">
          @for (p of presets; track p.key) {
            <button
              class="chip"
              [class.chip--on]="activeKey() === p.key"
              [disabled]="busy() || !!storm()"
              [title]="p.hint"
              (click)="toggle(p)"
            >
              {{ p.label }}
            </button>
          }
          <button
            class="chip chip--off"
            [disabled]="busy() || !active() || !!storm()"
            (click)="clear()"
          >
            Off
          </button>
        </div>
      </div>

      <div class="grp">
        <div class="grp__hd">Timed storms</div>
        <div class="storms">
          @for (s of storms; track s.key) {
            <button
              class="storm-btn"
              [disabled]="busy() || !!storm()"
              (click)="runStorm(s)"
            >
              <span class="storm-btn__l">{{ s.label }}</span>
              <span class="storm-btn__h muted">{{ s.hint }}</span>
            </button>
          }
        </div>
      </div>
    </section>
  `,
  styles: [
    `
      :host {
        display: block;
      }
      .muted {
        color: var(--text-muted);
      }
      .sm {
        font-size: var(--text-sm);
      }
      .mono {
        font-family: var(--font-mono);
      }

      .panel {
        background: var(--bg-surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        padding: var(--space-4);
        box-shadow: var(--shadow-xs);
      }
      .chaos {
        border-color: color-mix(
          in srgb,
          var(--color-warning) 30%,
          var(--border)
        );
      }
      .panel__head {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        margin-bottom: var(--space-1);
      }
      .panel__head h3 {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        margin: 0;
        font-size: var(--text-base);
      }
      .faults {
        margin-left: auto;
        font-variant-numeric: tabular-nums;
      }
      .intro {
        margin: 0 0 var(--space-3);
        max-width: 62ch;
      }

      .storm {
        display: flex;
        align-items: center;
        gap: var(--space-2);
        margin-bottom: var(--space-3);
        padding: var(--space-2) var(--space-3);
        border-radius: var(--radius-md);
        background: color-mix(in srgb, var(--color-warning) 12%, transparent);
        border: 1px solid
          color-mix(in srgb, var(--color-warning) 40%, var(--border));
        font-size: var(--text-sm);
      }
      .storm__pulse {
        width: 9px;
        height: 9px;
        border-radius: 50%;
        background: var(--color-warning);
        flex: none;
        animation: chaos-pulse 1s ease-in-out infinite;
      }
      @keyframes chaos-pulse {
        0%,
        100% {
          opacity: 1;
        }
        50% {
          opacity: 0.3;
        }
      }
      .storm__txt b {
        color: var(--text-primary);
      }
      .storm__t {
        margin-left: auto;
        color: var(--text-secondary);
        font-variant-numeric: tabular-nums;
      }
      .storm__stop {
        border: none;
        background: var(--color-danger);
        color: #fff;
        cursor: pointer;
        border-radius: var(--radius-sm);
        padding: 2px var(--space-2-5);
        font-size: var(--text-xs);
      }

      .grp {
        margin-top: var(--space-3);
      }
      .grp__hd {
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
        color: var(--text-muted);
        margin-bottom: var(--space-2);
      }
      .chips {
        display: flex;
        flex-wrap: wrap;
        gap: var(--space-2);
      }
      .chip {
        border: 1px solid var(--border);
        background: var(--bg-base);
        color: var(--text-secondary);
        border-radius: var(--radius-full);
        padding: 4px var(--space-3);
        cursor: pointer;
        font-size: var(--text-sm);
      }
      .chip:hover:not(:disabled) {
        background: var(--bg-subtle);
        color: var(--text-primary);
      }
      .chip:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .chip--on {
        background: color-mix(in srgb, var(--color-warning) 18%, transparent);
        border-color: var(--color-warning);
        color: var(--text-primary);
        font-weight: var(--weight-medium);
      }
      .chip--off {
        color: var(--text-muted);
      }

      .storms {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: var(--space-2);
      }
      .storm-btn {
        display: flex;
        flex-direction: column;
        gap: 2px;
        text-align: left;
        cursor: pointer;
        border: 1px solid var(--border);
        background: var(--bg-base);
        border-radius: var(--radius-md);
        padding: var(--space-2) var(--space-3);
      }
      .storm-btn:hover:not(:disabled) {
        background: var(--bg-subtle);
        border-color: var(--border-strong);
      }
      .storm-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .storm-btn__l {
        font-size: var(--text-sm);
        font-weight: var(--weight-medium);
      }
      .storm-btn__h {
        font-size: var(--text-xs);
      }
    `,
  ],
})
export class ChaosDialComponent implements OnDestroy {
  private readonly api = inject(RunApiService);
  private readonly ui = inject(UiService);

  /** Running simulator id. */
  readonly simId = input.required<string>();
  /** Only poll/act while the simulator is actually running. */
  readonly running = input(true);

  readonly presets = PRESETS;
  readonly storms = STORMS;

  readonly status = signal<ChaosStatus | null>(null);
  readonly busy = signal(false);

  readonly chaos = computed(() => this.status()?.chaos ?? {});
  readonly active = computed(() =>
    Object.keys(this.chaos()).some((k) => k !== "seed"),
  );
  readonly storm = computed(() => this.status()?.storm ?? null);
  /** Which preset (if any) matches the live block — for the pressed chip. */
  readonly activeKey = computed(() => {
    const c = stripSeed(this.chaos());
    if (!Object.keys(c).length) return null;
    const hit = PRESETS.find((p) => sameChaos(p.chaos, c));
    return hit?.key ?? "custom";
  });

  private readonly poll = new PagePoll(2000, () => this.refresh());

  constructor() {
    // (re)start polling when a running simulator id is available; PagePoll
    // fires immediately and skips ticks while the tab is hidden.
    effect(() => {
      const id = this.simId();
      const on = this.running();
      this.poll.stop();
      this.status.set(null);
      if (id && on) this.poll.start();
    });
  }
  ngOnDestroy() {
    this.poll.stop();
  }

  private refresh() {
    this.api.simulatorChaos(this.simId()).subscribe({
      next: (s) => this.status.set(s),
      error: () => {},
    });
  }

  toggle(p: Preset) {
    if (this.activeKey() === p.key) {
      this.clear();
      return;
    }
    this.apply({ ...p.chaos, seed: 1 }, `${p.label} on`);
  }
  clear() {
    this.apply({}, "Chaos off");
  }
  private apply(chaos: ChaosBlock, msg: string) {
    this.busy.set(true);
    this.api.setSimulatorChaos(this.simId(), chaos).subscribe({
      next: (s) => {
        this.status.set(s);
        this.busy.set(false);
        this.ui.toast("success", msg);
      },
      error: () => {
        this.busy.set(false);
        this.ui.toast("error", "Could not update chaos.");
      },
    });
  }

  runStorm(s: Storm) {
    this.busy.set(true);
    this.api
      .startChaosStorm(this.simId(), { phases: s.phases, repeat: s.repeat })
      .subscribe({
        next: (st) => {
          this.status.set(st);
          this.busy.set(false);
          this.ui.toast("success", `${s.label} storm started.`);
        },
        error: () => {
          this.busy.set(false);
          this.ui.toast("error", "Could not start storm.");
        },
      });
  }
  stopStorm() {
    this.busy.set(true);
    this.api.cancelChaosStorm(this.simId()).subscribe({
      next: () => {
        this.busy.set(false);
        this.ui.toast("success", "Storm stopped; baseline restored.");
        this.refresh();
      },
      error: () => {
        this.busy.set(false);
        this.ui.toast("error", "Could not stop storm.");
      },
    });
  }
}

// -- pure comparison helpers -------------------------------------------------
function stripSeed(c: ChaosBlock): ChaosBlock {
  const { seed: _seed, ...rest } = c;
  return rest;
}
function sameChaos(a: ChaosBlock, b: ChaosBlock): boolean {
  return JSON.stringify(canon(a)) === JSON.stringify(canon(b));
}
/** Stable key order so two equivalent blocks compare equal via JSON. */
function canon(c: ChaosBlock): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(stripSeed(c)).sort()) {
    const v = (c as Record<string, unknown>)[k];
    out[k] = v && typeof v === "object" ? canon(v as ChaosBlock) : v;
  }
  return out;
}
