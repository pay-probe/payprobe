import { GraphEdge, GraphNode } from "../../run-monitor/run-api.service";
import type { NetworkGraph } from "../../run-monitor/run-api.service";

/** Poll cadence — one frame every POLL_MS while live. */
export const POLL_MS = 2000;
/** Rolling buffer size (~8 min of history at 2s polls). */
export const MAX_FRAMES = 240;
export const STRIP_W = 600;
export const STRIP_H = 44;
export const SPARK_W = 240;
export const SPARK_H = 40;

/** One recorded poll of the network — the unit of time travel. */
export interface Frame {
  seq: number;
  at: number;
  g: NetworkGraph;
  /** per-node msg/s derived from the previous frame. */
  rates: Map<string, number>;
  /** nodes that saw traffic since the previous frame. */
  actives: Set<string>;
  tps: number;
}

/** A notable change between two frames, plotted on the scrubber. */
export interface Evt {
  key: number;
  seq: number;
  time: string;
  text: string;
  tone: "ok" | "warn" | "bad" | "info";
  nodeId: string;
}

/** An event dot on the scrubber strip. */
export interface StripMark {
  key: number;
  x: number;
  tone: string;
  text: string;
}

export interface RNode extends GraphNode {
  x: number;
  y: number;
  rate: number;
  active: boolean;
  health: "ok" | "warn" | "down" | "idle";
  /** rate momentum vs the previous frame. */
  trend: "up" | "down" | "flat";
}
export interface REdge extends GraphEdge {
  d: string;
  active: boolean;
}
export interface MixRow {
  k: string;
  v: number;
  w: number; // bar width 0..100
  ok: boolean;
}

/** On-disk shape of an exported replay (chronoscope-replay-*.json). */
export interface ReplayFile {
  exported_at?: string;
  poll_ms?: number;
  frames: { seq?: number; at?: number; tps?: number; graph: NetworkGraph }[];
  events?: {
    seq?: number;
    time?: string;
    text?: string;
    tone?: string;
    node?: string;
  }[];
}

// -- pure helpers ------------------------------------------------------------
export function okCount(n: GraphNode): number {
  let ok = 0;
  for (const [k, v] of Object.entries(n.by_rc ?? {})) if (k === "00") ok += v;
  return ok;
}
export function declCount(n: GraphNode): number {
  let d = 0;
  for (const [k, v] of Object.entries(n.by_rc ?? {})) if (k !== "00") d += v;
  return d;
}
export function seriesPoints(vals: number[], w: number, h: number): string {
  if (!vals.length) return "";
  const max = Math.max(1, ...vals);
  const n = Math.max(1, vals.length - 1);
  return vals
    .map(
      (v, i) =>
        ((i / n) * w).toFixed(1) + "," + (h - (v / max) * (h - 6)).toFixed(1),
    )
    .join(" ");
}
export function mixRows(
  rec: Record<string, number> | undefined,
  isOk: (k: string) => boolean,
): MixRow[] {
  if (!rec) return [];
  const rows = Object.entries(rec)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
  const max = Math.max(1, ...rows.map(([, v]) => v));
  return rows.map(([k, v]) => ({
    k,
    v,
    w: Math.max(3, Math.round((v / max) * 100)),
    ok: isOk(k),
  }));
}
/** Function icon per node kind (matches the sidebar tile icons). */
export function kindIcon(kind: string): string {
  switch (kind) {
    case "participant":
      return "flow";
    case "simulator":
      return "server";
    case "group":
      return "group";
    case "driver":
      return "play";
    case "clients":
      return "user";
    case "host":
      return "cpu";
    default:
      return "plug";
  }
}
export function nodeSub(n: GraphNode): string {
  if (n.kind === "driver") return "topology driver";
  if (n.kind === "clients") return n.sublabel || "inbound";
  if (n.kind === "host") return n.sublabel || "external";
  const proto = n.protocol || n.sublabel || "";
  if (n.port) return proto + " :" + n.port;
  return n.status === "down" ? "stopped" : proto;
}
