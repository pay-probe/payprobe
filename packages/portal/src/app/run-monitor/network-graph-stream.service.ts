import { Injectable, inject, signal } from "@angular/core";

import { AuthService } from "../auth/auth.service";
import { PollHealth } from "../shared/poll-health";
import { RuntimeConfigService } from "../shared/runtime-config.service";
import { NetworkGraph, RunApiService } from "./run-api.service";

/**
 * One shared feed of GET /network-graph for every live view (Live Network
 * Map, Chronoscope, …) — instead of each page running its own 2s poll loop.
 *
 * Transport: prefers the SSE push stream (/network-graph/stream, opened via
 * fetch so the Bearer header rides along — EventSource can't send headers),
 * and falls back to 2s polling whenever the stream can't be opened or drops.
 * A 404/405 (older orchestrator) disables further SSE attempts this session.
 *
 * Lifecycle is refcounted: pages call acquire() in ngOnInit and release() in
 * ngOnDestroy; the feed stops when the last consumer leaves. Connection truth
 * lives in {@link pollHealth} — consumers render Stale from it.
 */
@Injectable({ providedIn: "root" })
export class NetworkGraphStreamService {
  private readonly api = inject(RunApiService);
  private readonly cfg = inject(RuntimeConfigService);
  private readonly auth = inject(AuthService);

  /** Latest snapshot — components derive rates/layout from successive values. */
  readonly graph = signal<NetworkGraph | null>(null);
  /** "sse" while the push stream is live, "poll" on the fallback loop. */
  readonly transport = signal<"sse" | "poll">("poll");
  readonly pollHealth = new PollHealth();
  readonly stale = this.pollHealth.stale;

  private refs = 0;
  private abort: AbortController | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  /** Older orchestrator without the stream endpoint — stop trying SSE. */
  private sseUnavailable = false;

  acquire(): void {
    if (++this.refs === 1) this.start();
  }
  release(): void {
    this.refs = Math.max(0, this.refs - 1);
    if (this.refs === 0) this.stop();
  }

  /** One-shot fetch (manual Refresh buttons) — works even while streaming. */
  refreshOnce(): void {
    this.api.networkGraph().subscribe({
      next: (g) => {
        this.graph.set(g);
        this.pollHealth.markOk();
      },
      error: () => this.pollHealth.markError(),
    });
  }

  // -- internals -----------------------------------------------------------

  private start(): void {
    this.abort = new AbortController();
    void this.runLoop(this.abort.signal);
  }

  private stop(): void {
    this.abort?.abort();
    this.abort = null;
    this.stopPolling();
    this.pollHealth.destroy();
  }

  private async runLoop(signal: AbortSignal): Promise<void> {
    while (!signal.aborted) {
      if (this.sseUnavailable) {
        this.ensurePolling();
        return;
      }
      try {
        await this.consumeStream(signal);
        // clean end (server restarted / connection recycled) → reconnect
      } catch {
        if (signal.aborted) return;
        this.pollHealth.markError();
      }
      if (signal.aborted) return;
      // while SSE is down, poll so the board keeps breathing during retries
      this.ensurePolling();
      await new Promise((r) => setTimeout(r, 2000));
    }
  }

  /** Opens the SSE stream and applies frames until it ends or errors. */
  private async consumeStream(signal: AbortSignal): Promise<void> {
    const token = this.auth.token();
    const res = await fetch(`${this.cfg.runApiBase}/network-graph/stream`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal,
    });
    if (res.status === 404 || res.status === 405) {
      this.sseUnavailable = true;
      throw new Error("stream endpoint unavailable");
    }
    if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);

    this.transport.set("sse");
    this.stopPolling();

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try {
            this.graph.set(JSON.parse(payload) as NetworkGraph);
            this.pollHealth.markOk();
          } catch {
            /* ignore a malformed frame */
          }
        }
      }
    }
  }

  private ensurePolling(): void {
    if (this.pollTimer) return;
    this.transport.set("poll");
    const tick = () => {
      this.api.networkGraph().subscribe({
        next: (g) => {
          this.graph.set(g);
          this.pollHealth.markOk();
        },
        error: () => this.pollHealth.markError(),
      });
    };
    tick();
    this.pollTimer = setInterval(tick, 2000);
  }

  private stopPolling(): void {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }
}
