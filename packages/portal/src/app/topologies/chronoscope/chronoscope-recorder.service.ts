import { Injectable, effect, inject, signal } from "@angular/core";

import { NetworkGraphStreamService } from "../../run-monitor/network-graph-stream.service";
import { NetworkGraph } from "../../run-monitor/run-api.service";
import {
  Evt,
  Frame,
  MAX_FRAMES,
  POLL_MS,
  ReplayFile,
  declCount,
  okCount,
} from "./chronoscope.model";

/**
 * The Chronoscope's tape deck. Owns the rolling frame buffer and the
 * chronicle: records frames off the shared network-graph feed (deriving
 * per-node rates and frame-diff events), exports the buffer as a shareable
 * replay file, and can load such a file back for offline review ("replay"
 * source).
 *
 * Root-provided so the tape SURVIVES NAVIGATION: it records whenever any map
 * page holds the shared stream, and "it was fine a minute ago, scrub back"
 * works even if that minute was spent on the Live Network Map. Rate math uses
 * real frame timestamps, so gaps (no map open) average correctly instead of
 * lying.
 */
@Injectable({ providedIn: "root" })
export class ChronoscopeRecorderService {
  private readonly stream = inject(NetworkGraphStreamService);

  readonly frames = signal<Frame[]>([]);
  readonly events = signal<Evt[]>([]);
  /** live = recording the feed; replay = browsing an imported file. */
  readonly source = signal<"live" | "replay">("live");
  readonly replayName = signal<string | null>(null);
  /** Buffer-trim counter — a view holding a past frame re-anchors on change. */
  readonly trims = signal(0);

  /** Tape head: records each new feed frame while in live mode. */
  private lastRecorded: NetworkGraph | null = null;
  private readonly recordFx = effect(() => {
    const g = this.stream.graph();
    if (!g || g === this.lastRecorded || this.source() !== "live") return;
    this.lastRecorded = g;
    if (this.record(g)) this.trims.update((n) => n + 1);
  });

  private seq = 0;
  private evtKey = 0;
  /** consecutive zero-rate frames per listener node (went-quiet detection). */
  private silentStreak = new Map<string, number>();
  private lastGoodRate = new Map<string, number>();

  /** Record a live poll. Returns true when the buffer dropped its oldest frame. */
  record(g: NetworkGraph): boolean {
    if (this.source() !== "live") return false;
    const now = Date.now();
    const fr = this.frames();
    const prev = fr[fr.length - 1] ?? null;
    const frameSeq = this.seq++;
    const { rates, actives, tps } = deriveRates(prev, g, now);
    if (prev) {
      this.detectEvents(prev.g, g, frameSeq);
      this.detectSilence(g, rates, frameSeq);
    }
    const frame: Frame = { seq: frameSeq, at: now, g, rates, actives, tps };
    const trimmed = fr.length >= MAX_FRAMES;
    this.frames.update((cur) => [...cur, frame].slice(-MAX_FRAMES));
    return trimmed;
  }

  /** Wipe the tape and go back to recording live polls. */
  reset(): void {
    this.frames.set([]);
    this.events.set([]);
    this.source.set("live");
    this.replayName.set(null);
    this.seq = 0;
    this.evtKey = 0;
    this.silentStreak.clear();
    this.lastGoodRate.clear();
  }

  /** Download the whole buffer + chronicle as a shareable replay file. */
  exportReplay(): void {
    const data: ReplayFile = {
      exported_at: new Date().toISOString(),
      poll_ms: POLL_MS,
      frames: this.frames().map((f) => ({
        seq: f.seq,
        at: f.at,
        tps: f.tps,
        graph: f.g,
      })),
      events: this.events().map((e) => ({
        seq: e.seq,
        time: e.time,
        text: e.text,
        tone: e.tone,
        node: e.nodeId,
      })),
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      "chronoscope-replay-" +
      new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") +
      ".json";
    a.click();
    URL.revokeObjectURL(url);
  }

  /** Load an exported replay file and switch to browsing it offline. */
  async importReplay(file: File): Promise<void> {
    let data: ReplayFile;
    try {
      data = JSON.parse(await file.text()) as ReplayFile;
    } catch {
      throw new Error("Not valid JSON");
    }
    if (!Array.isArray(data?.frames) || !data.frames.length) {
      throw new Error("Not a Chronoscope replay file (no frames)");
    }
    const frames: Frame[] = [];
    let prev: Frame | null = null;
    data.frames.forEach((raw, i) => {
      const g = raw.graph;
      if (!g || !Array.isArray(g.nodes) || !Array.isArray(g.edges)) {
        throw new Error("Frame " + i + " has no graph");
      }
      const at =
        typeof raw.at === "number" ? raw.at : i * (data.poll_ms ?? POLL_MS);
      const { rates, actives, tps } = deriveRates(prev, g, at);
      const f: Frame = {
        seq: typeof raw.seq === "number" ? raw.seq : i,
        at,
        g,
        rates,
        actives,
        tps: typeof raw.tps === "number" ? raw.tps : tps,
      };
      frames.push(f);
      prev = f;
    });
    const tones = new Set(["ok", "warn", "bad", "info"]);
    const events: Evt[] = (data.events ?? []).map((e, i) => ({
      key: i,
      seq: e.seq ?? 0,
      time: e.time ?? "",
      text: e.text ?? "",
      tone: tones.has(e.tone ?? "") ? (e.tone as Evt["tone"]) : "info",
      nodeId: e.node ?? "",
    }));
    this.frames.set(frames);
    this.events.set(events);
    this.source.set("replay");
    this.replayName.set(file.name);
  }

  // -- event detection ---------------------------------------------------------
  private addEvent(
    seq: number,
    nodeId: string,
    text: string,
    tone: Evt["tone"],
  ) {
    this.events.update((cur) =>
      [
        ...cur,
        {
          key: this.evtKey++,
          seq,
          time: new Date().toLocaleTimeString(),
          text,
          tone,
          nodeId,
        },
      ].slice(-250),
    );
  }

  private detectEvents(prev: NetworkGraph, next: NetworkGraph, seq: number) {
    const watched = (k: string) =>
      k === "participant" || k === "simulator" || k === "host";
    const prevById = new Map(prev.nodes.map((n) => [n.id, n]));
    const nextIds = new Set(next.nodes.map((n) => n.id));

    for (const n of next.nodes) {
      const p = prevById.get(n.id);
      if (!p) {
        if (watched(n.kind))
          this.addEvent(seq, n.id, n.label + " joined the network", "info");
        continue;
      }
      if (p.status !== "down" && n.status === "down")
        this.addEvent(seq, n.id, n.label + " went down", "bad");
      else if (p.status === "down" && n.status === "up")
        this.addEvent(seq, n.id, n.label + " back up", "ok");
      const dDecl = declCount(n) - declCount(p);
      const dOk = okCount(n) - okCount(p);
      if (dDecl >= 3 && dDecl > dOk)
        this.addEvent(
          seq,
          n.id,
          n.label + " decline burst (+" + dDecl + ")",
          "warn",
        );
    }
    for (const p of prev.nodes) {
      if (!nextIds.has(p.id) && watched(p.kind))
        this.addEvent(seq, p.id, p.label + " left the network", "info");
    }
  }

  /** A busy listener falling silent while still "up" is often the real signal. */
  private detectSilence(
    g: NetworkGraph,
    rates: Map<string, number>,
    seq: number,
  ) {
    for (const n of g.nodes) {
      if (n.kind !== "participant" && n.kind !== "simulator") continue;
      const r = rates.get(n.id) || 0;
      if (r > 0) {
        this.silentStreak.set(n.id, 0);
        this.lastGoodRate.set(n.id, r);
        continue;
      }
      const streak = (this.silentStreak.get(n.id) || 0) + 1;
      this.silentStreak.set(n.id, streak);
      const was = this.lastGoodRate.get(n.id) || 0;
      if (streak === 3 && was >= 5 && n.status === "up") {
        this.addEvent(
          seq,
          n.id,
          n.label + " went quiet (was " + was + "/s)",
          "warn",
        );
        this.lastGoodRate.set(n.id, 0); // one event per silence episode
      }
    }
  }
}

/** Per-node msg/s + active set + total tps, derived from the previous frame. */
function deriveRates(
  prev: Frame | null,
  g: NetworkGraph,
  at: number,
): { rates: Map<string, number>; actives: Set<string>; tps: number } {
  const rates = new Map<string, number>();
  const actives = new Set<string>();
  let tps = 0;
  if (prev) {
    const dt = Math.max(0.5, (at - prev.at) / 1000);
    const prevById = new Map(prev.g.nodes.map((n) => [n.id, n]));
    for (const n of g.nodes) {
      const p = prevById.get(n.id);
      if (p && n.received > p.received) {
        const rate = Math.round((n.received - p.received) / dt);
        rates.set(n.id, rate);
        actives.add(n.id);
        tps += rate;
      }
    }
  }
  return { rates, actives, tps: Math.round(tps) };
}
