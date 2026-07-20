import { Injectable, effect, inject, signal } from "@angular/core";

import { NetworkGraphStreamService } from "../run-monitor/network-graph-stream.service";
import { ChronoscopeRecorderService } from "../topologies/chronoscope/chronoscope-recorder.service";

export interface NetworkAlert {
  key: number;
  time: string;
  text: string;
  tone: "warn" | "bad";
  nodeId: string;
}

const NOTIFY_KEY = "pp.alerts.notify";

/**
 * App-wide network alerting — the piece that turns "monitoring you watch"
 * into "monitoring that watches". The Chronoscope recorder already detects
 * events (went down, decline burst, went quiet) off the shared graph feed;
 * this service holds the feed open for the whole session (one SSE connection
 * — the deliberate cost of being told), collects the warn/bad events into a
 * topbar bell, and optionally raises browser notifications while the tab is
 * hidden. Events from an imported replay file are history, never alerts.
 */
@Injectable({ providedIn: "root" })
export class NetworkAlertsService {
  private readonly recorder = inject(ChronoscopeRecorderService);
  private readonly stream = inject(NetworkGraphStreamService);

  /** Newest first, capped at 30. */
  readonly alerts = signal<NetworkAlert[]>([]);
  readonly unseen = signal(0);
  /** Browser-notification opt-in (persisted). */
  readonly notify = signal(localStorage.getItem(NOTIFY_KEY) === "1");

  private lastKey = -1;
  private started = false;

  private readonly watchFx = effect(() => {
    const evts = this.recorder.events();
    if (this.recorder.source() !== "live") return;
    const maxKey = evts.length ? evts[evts.length - 1].key : -1;
    if (maxKey < this.lastKey) {
      this.lastKey = maxKey; // tape was reset — resync without alerting
      return;
    }
    const fresh = evts.filter(
      (e) => e.key > this.lastKey && (e.tone === "bad" || e.tone === "warn"),
    );
    this.lastKey = maxKey;
    if (!fresh.length) return;
    const mapped: NetworkAlert[] = fresh
      .map((e) => ({
        key: e.key,
        time: e.time,
        text: e.text,
        tone: e.tone as "warn" | "bad",
        nodeId: e.nodeId,
      }))
      .reverse(); // newest first
    this.alerts.update((cur) => [...mapped, ...cur].slice(0, 30));
    this.unseen.update((n) => n + mapped.length);
    if (this.notify() && document.hidden) {
      for (const a of mapped) this.raise(a);
    }
  });

  /** Idempotent — the topbar calls it once per session. */
  start(): void {
    if (this.started) return;
    this.started = true;
    this.stream.acquire();
  }

  markSeen(): void {
    this.unseen.set(0);
  }

  clear(): void {
    this.alerts.set([]);
    this.unseen.set(0);
  }

  /** Toggle browser notifications, requesting permission on first enable. */
  async toggleNotify(): Promise<void> {
    if (this.notify()) {
      this.notify.set(false);
      localStorage.setItem(NOTIFY_KEY, "0");
      return;
    }
    if (typeof Notification === "undefined") return;
    const perm =
      Notification.permission === "granted"
        ? "granted"
        : await Notification.requestPermission();
    if (perm === "granted") {
      this.notify.set(true);
      localStorage.setItem(NOTIFY_KEY, "1");
    }
  }

  private raise(a: NetworkAlert): void {
    try {
      new Notification("PayProbe — network event", {
        body: a.text,
        tag: "pp-alert-" + a.key, // dedupe per event
      });
    } catch {
      /* notification constructor can throw on some platforms — never fatal */
    }
  }
}
