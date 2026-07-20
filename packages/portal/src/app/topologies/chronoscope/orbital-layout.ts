import { GraphNode } from "../../run-monitor/run-api.service";
import { Frame, REdge, RNode } from "./chronoscope.model";
import { buildAdjacency } from "../tm2-layout";

const TRIM = 34; // edge endpoint inset from node centers
/** Arc length reserved per node so circles + centred labels never collide. */
const MIN_ARC = 150;
/** Minimum radial distance between adjacent rings (node + label + stat). */
const RING_GAP = 110;
const DEG = Math.PI / 180;

export interface OrbitalLayout {
  nodes: RNode[];
  edges: REdge[];
  rings: number[];
  w: number;
  h: number;
  cx: number;
  cy: number;
  sweepR: number;
}

const EMPTY: OrbitalLayout = {
  nodes: [],
  edges: [],
  rings: [],
  w: 760,
  h: 560,
  cx: 380,
  cy: 280,
  sweepR: 0,
};

/**
 * Radar-scope layout: sims/hosts at the core (a lone core node sits
 * dead-center), participants/groups on the middle ring, initiators/drivers/
 * clients on the outer ring. Chord edges bend toward the center so crossings
 * read as orbital arcs. Pure — depends only on the frame (and the previous
 * frame, for rate-trend arrows).
 *
 * Angular alignment (same treatment as the map-2 lane layout, sharing its
 * adjacency helper): instead of alphabetical spacing, each ring is ordered
 * and rotated by iterative circular-barycenter passes, so connected nodes
 * line up on spokes and chords stay short. Ring radii additionally grow
 * with occupancy so a crowded ring never overlaps its labels.
 */
export function computeOrbitalLayout(
  frame: Frame | null,
  prev: Frame | null,
): OrbitalLayout {
  if (!frame || !frame.g.nodes.length) return EMPTY;
  const g = frame.g;

  const ringOf = (k: string): number =>
    k === "simulator" || k === "host"
      ? 0
      : k === "initiator" || k === "driver" || k === "clients"
        ? 2
        : 1;

  const groups = new Map<number, GraphNode[]>();
  for (const n of g.nodes) {
    const r = ringOf(n.kind);
    groups.set(r, [...(groups.get(r) || []), n]);
  }
  const ranks = [...groups.keys()].sort((a, b) => a - b);
  const RADII: Record<number, number[]> = {
    1: [190],
    2: [130, 300],
    3: [100, 220, 340],
  };
  const radii = RADII[ranks.length] || [190];
  const ringR = new Map<number, number>();
  ranks.forEach((rk, i) => ringR.set(rk, radii[i]));
  // a lone innermost node sits dead-center
  const innerList = groups.get(ranks[0])!;
  if (innerList.length === 1 && ranks.length > 1) ringR.set(ranks[0], 0);

  // grow rings with occupancy, keeping them concentric and separated
  let prevR = 0;
  ranks.forEach((rk) => {
    let r = ringR.get(rk)!;
    if (r === 0) return; // the dead-center slot
    const count = groups.get(rk)!.length;
    r = Math.max(r, (count * MIN_ARC) / (2 * Math.PI), prevR + RING_GAP);
    ringR.set(rk, r);
    prevR = r;
  });

  const maxR = Math.ceil(Math.max(...ringR.values(), 100));
  const w = Math.max(760, 2 * (maxR + 170));
  const h = Math.max(560, 2 * (maxR + 120));
  const cx = w / 2;
  const cy = h / 2;

  const order = [
    "initiator",
    "driver",
    "clients",
    "group",
    "participant",
    "host",
    "simulator",
  ];
  const byKind = (a: GraphNode, b: GraphNode) =>
    order.indexOf(a.kind) - order.indexOf(b.kind) ||
    a.label.localeCompare(b.label);

  // initial angles: deterministic kind/label order, evenly spaced
  const ang = new Map<string, number>();
  ranks.forEach((rk, ri) => {
    const list = groups.get(rk)!;
    list.sort(byKind);
    const start = -90 + ri * 24; // stagger rings so spokes don't stack
    const step = 360 / list.length;
    list.forEach((n, i) => ang.set(n.id, start + i * step));
  });

  // angular alignment: pull every node toward the circular mean of its
  // neighbours (any ring, either direction), then re-slot the ring evenly
  // in that order with the best-fit rotation. Even spacing keeps the radar
  // look; the ordering + rotation give the spoke alignment.
  const { incoming, outgoing } = buildAdjacency(g.nodes, g.edges);
  const neigh = (id: string) => [
    ...(incoming.get(id) || []),
    ...(outgoing.get(id) || []),
  ];
  for (let pass = 0; pass < 6; pass++) {
    const seq = pass % 2 === 0 ? ranks : [...ranks].reverse();
    for (const rk of seq) {
      const list = groups.get(rk)!;
      if (ringR.get(rk) === 0 || list.length < 2) continue;
      const desired = new Map<string, number>();
      for (const n of list) {
        const ns = neigh(n.id);
        if (!ns.length) {
          desired.set(n.id, ang.get(n.id)!);
          continue;
        }
        let sx = 0;
        let sy = 0;
        for (const id of ns) {
          const a = ang.get(id)! * DEG;
          sx += Math.cos(a);
          sy += Math.sin(a);
        }
        desired.set(n.id, sx || sy ? Math.atan2(sy, sx) / DEG : ang.get(n.id)!);
      }
      const sorted = [...list].sort(
        (a, b) => desired.get(a.id)! - desired.get(b.id)! || byKind(a, b),
      );
      const step = 360 / sorted.length;
      // rotation = circular mean of the per-slot offsets to the desires
      let rx = 0;
      let ry = 0;
      sorted.forEach((n, i) => {
        const off = (desired.get(n.id)! - i * step) * DEG;
        rx += Math.cos(off);
        ry += Math.sin(off);
      });
      const rot = rx || ry ? Math.atan2(ry, rx) / DEG : -90;
      sorted.forEach((n, i) => ang.set(n.id, rot + i * step));
      groups.set(rk, sorted);
    }
  }

  const pos = new Map<string, { x: number; y: number }>();
  ranks.forEach((rk) => {
    const list = groups.get(rk)!;
    const r = ringR.get(rk)!;
    if (r === 0) {
      pos.set(list[0].id, { x: cx, y: cy });
      return;
    }
    for (const n of list) {
      const a = ang.get(n.id)! * DEG;
      pos.set(n.id, {
        x: Math.round(cx + r * Math.cos(a)),
        y: Math.round(cy + r * Math.sin(a)),
      });
    }
  });

  const nodes: RNode[] = g.nodes.map((n) => {
    const p = pos.get(n.id)!;
    const rate = frame.rates.get(n.id) || 0;
    let rcTotal = 0;
    let ok = 0;
    if (n.by_rc) {
      for (const [k, v] of Object.entries(n.by_rc)) {
        rcTotal += v;
        if (k === "00") ok += v;
      }
    }
    const declShare = rcTotal ? 1 - ok / rcTotal : 0;
    let health: RNode["health"] = "idle";
    if (n.status === "down") health = "down";
    else if (rcTotal && declShare > 0.25) health = "warn";
    else if (n.status === "up") health = "ok";
    const prevRate = prev?.rates.get(n.id) ?? 0;
    const diff = rate - prevRate;
    const thr = Math.max(2, prevRate * 0.2);
    const trend: RNode["trend"] =
      rate === 0 && prevRate === 0
        ? "flat"
        : diff > thr
          ? "up"
          : diff < -thr
            ? "down"
            : "flat";
    return {
      ...n,
      x: p.x,
      y: p.y,
      rate,
      active: frame.actives.has(n.id),
      health,
      trend,
    };
  });

  const edges: REdge[] = [];
  for (const e of g.edges) {
    const s = pos.get(e.source);
    const t = pos.get(e.target);
    if (!s || !t) continue;
    const dx = t.x - s.x;
    const dy = t.y - s.y;
    const len = Math.hypot(dx, dy) || 1;
    const ux = dx / len;
    const uy = dy / len;
    const ax = s.x + ux * TRIM;
    const ay = s.y + uy * TRIM;
    const bx = t.x - ux * TRIM;
    const by = t.y - uy * TRIM;
    // bend chords toward the core so crossings read as orbital arcs
    const mx = (ax + bx) / 2;
    const my = (ay + by) / 2;
    const k = 0.32;
    const qx = mx + (cx - mx) * k;
    const qy = my + (cy - my) * k;
    const d =
      "M " +
      ax +
      " " +
      ay +
      " Q " +
      qx.toFixed(1) +
      " " +
      qy.toFixed(1) +
      " " +
      bx +
      " " +
      by;
    const active = frame.actives.has(e.target) || frame.actives.has(e.source);
    edges.push({ ...e, d, active });
  }

  const rings = ranks.map((rk) => ringR.get(rk)!).filter((r) => r > 0);
  return { nodes, edges, rings, w, h, cx, cy, sweepR: maxR + 30 };
}
