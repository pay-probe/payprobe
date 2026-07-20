import { computed, signal } from "@angular/core";

/**
 * Tracks the outcome of a polling loop so live views can tell the truth when
 * the backend stops answering. Without this, a frozen map keeps its "Live"
 * badge on stale data — the worst failure mode for a monitoring page.
 *
 * Usage: call markOk()/markError() from the poll subscription; render from
 * stale() and sinceOk(). Call destroy() in ngOnDestroy (stops the 1s clock
 * that only runs while degraded).
 */
export class PollHealth {
  private readonly failures = signal(0);
  private readonly lastOkAt = signal<number | null>(null);
  /** 1s heartbeat so sinceOk() re-evaluates; only ticks while degraded. */
  private readonly tick = signal(0);
  private clock: ReturnType<typeof setInterval> | null = null;

  constructor(private readonly threshold = 2) {}

  /** True after `threshold` consecutive failed polls. One blip stays quiet. */
  readonly stale = computed(() => this.failures() >= this.threshold);

  /** Seconds since the last successful poll (null before the first one). */
  readonly sinceOk = computed(() => {
    this.tick();
    const at = this.lastOkAt();
    return at === null
      ? null
      : Math.max(0, Math.round((Date.now() - at) / 1000));
  });

  markOk(): void {
    this.failures.set(0);
    this.lastOkAt.set(Date.now());
    this.stopClock();
  }

  markError(): void {
    this.failures.update((n) => n + 1);
    this.startClock();
  }

  destroy(): void {
    this.stopClock();
  }

  private startClock(): void {
    if (this.clock) return;
    this.clock = setInterval(() => this.tick.update((n) => n + 1), 1000);
  }
  private stopClock(): void {
    if (this.clock) {
      clearInterval(this.clock);
      this.clock = null;
    }
  }
}

/**
 * A page's polling loop, done right once: fires immediately, repeats on an
 * interval, and SKIPS ticks while the tab is hidden (a background tab doesn't
 * need fresh data, the server doesn't need the traffic). On becoming visible
 * again it fires straight away, so returning to the tab never shows a stale
 * beat. The tick callback owns its own request and should report the outcome
 * to a {@link PollHealth}.
 */
export class PagePoll {
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly onVis = () => {
    if (!document.hidden && this.timer) this.tick();
  };

  constructor(
    private readonly intervalMs: number,
    private readonly tick: () => void,
  ) {}

  start(): void {
    if (this.timer) return;
    this.tick();
    this.timer = setInterval(() => {
      if (!document.hidden) this.tick();
    }, this.intervalMs);
    document.addEventListener("visibilitychange", this.onVis);
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    document.removeEventListener("visibilitychange", this.onVis);
  }
}
