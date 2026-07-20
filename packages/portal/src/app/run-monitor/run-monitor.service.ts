import { Injectable, inject } from "@angular/core";
import { Observable, filter, map } from "rxjs";

import { AuthService } from "../auth/auth.service";
import { RuntimeConfigService } from "../shared/runtime-config.service";
import { PhaseUpdate, RunEvent, StepResult } from "./run.models";

/**
 * Live run monitor over WebSocket with automatic resume.
 *
 * Why not a plain RxJS `webSocket()`? Because we need durable resume: every
 * frame carries an `id`, and on an unexpected drop we reconnect with
 * `?last_event_id=<id>` so the orchestrator replays exactly what we missed
 * (backed by Redis Streams). The socket closes cleanly on `run.completed`.
 */
@Injectable({ providedIn: "root" })
export class RunMonitorService {
  private readonly auth = inject(AuthService);
  private readonly cfg = inject(RuntimeConfigService);

  /**
   * Build the WebSocket URL for a run by deriving it from `runApiBase`:
   * relative bases are resolved against the page origin, then the scheme is
   * swapped http(s)->ws(s). This keeps REST and the live stream on the same
   * host/prefix whether direct (dev) or behind nginx (prod).
   */
  private wsUrl(runId: string): string {
    const base = this.cfg.runApiBase;
    const httpUrl = /^https?:\/\//.test(base)
      ? base
      : `${location.origin}${base}`;
    const wsUrl = httpUrl.replace(/^http/, "ws");
    return `${wsUrl}/runs/${runId}/stream`;
  }

  /**
   * Stream events for a run. Emits each `RunEvent`, reconnects with backoff on
   * unexpected disconnects (resuming from the last seen id), and completes when
   * the run finishes.
   */
  stream(runId: string): Observable<RunEvent> {
    return new Observable<RunEvent>((subscriber) => {
      let lastId: string | null = null;
      let attempt = 0;
      let socket: WebSocket | null = null;
      let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
      let done = false;

      const open = () => {
        // A browser WebSocket can't set the Authorization header, so the
        // orchestrator's `authorize_websocket` accepts the bearer token as a
        // `?token=` query param. Without this the socket is rejected (1008) and
        // we'd reconnect forever, leaving the run stuck on "Waiting for results".
        const params = new URLSearchParams();
        const token = this.auth.token();
        if (token) params.set("token", token);
        if (lastId) params.set("last_event_id", lastId);
        const q = params.toString();
        socket = new WebSocket(`${this.wsUrl(runId)}${q ? `?${q}` : ""}`);

        socket.onmessage = (msg) => {
          attempt = 0; // a good frame resets backoff
          const event = JSON.parse(msg.data) as RunEvent;
          if (event.id) lastId = event.id;
          subscriber.next(event);
          if (event.type === "run.completed") {
            done = true;
            socket?.close();
            subscriber.complete();
          }
        };

        socket.onclose = (ev) => {
          if (done) return;
          // 1008 = policy violation: the server rejected our credentials.
          // Reconnecting with the same (missing/expired) token can't succeed,
          // so fail fast instead of spinning forever on "Waiting for results".
          if (ev.code === 1008) {
            done = true;
            subscriber.error(new Error("run stream unauthorized"));
            return;
          }
          const delay = Math.min(1000 * 2 ** attempt, 10_000);
          attempt += 1;
          reconnectTimer = setTimeout(open, delay);
        };

        socket.onerror = () => socket?.close();
      };

      open();

      return () => {
        done = true;
        if (reconnectTimer) clearTimeout(reconnectTimer);
        socket?.close();
      };
    });
  }

  /** Phase progress only. */
  phases(runId: string): Observable<PhaseUpdate> {
    return this.stream(runId).pipe(
      filter((e) => e.type === "phase.update"),
      map((e) => e.data as unknown as PhaseUpdate),
    );
  }

  /** Individual step results only. */
  steps(runId: string): Observable<StepResult> {
    return this.stream(runId).pipe(
      filter((e) => e.type === "step.result"),
      map((e) => e.data as unknown as StepResult),
    );
  }
}
