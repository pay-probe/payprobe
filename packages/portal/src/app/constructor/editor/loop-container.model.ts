/**
 * Loop containers and failure routes on the canvas (ADR-0014).
 *
 * Pure functions over steps / edges / rectangles, so the constructor component
 * only wires them to signals. Mirrors the server rules in
 * `scenario-service/models/scenario.py` (`_validate_containers`) and the
 * runner's route resolution (`worker/engine/runner.py`, `_failure_route`).
 */
import { Edge, FailurePolicy, Step } from "../scenario.models";

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ContainerBox extends Rect {
  loopId: string;
  /** 0 for a top-level loop, 1 for a loop inside it, … (render order). */
  depth: number;
  /** A fresh loop's drop zone: no members and no body wire yet. */
  empty?: boolean;
}

/** Size of a fresh loop's empty drop zone, right of the loop node. */
export const EMPTY_ZONE_W = 280;
export const EMPTY_ZONE_GAP = 40;

/** Space around the members, and the header strip that carries the label. */
export const BOX_PAD = 22;
export const BOX_HEAD = 30;

/** Ids of every step inside `loopId`'s container, nested loops included. */
export function descendantIds(steps: Step[], loopId: string): Set<string> {
  const out = new Set<string>();
  const todo = [loopId];
  while (todo.length) {
    const lid = todo.pop()!;
    for (const s of steps) {
      if (s.parent === lid && !out.has(s.id)) {
        out.add(s.id);
        todo.push(s.id);
      }
    }
  }
  return out;
}

function union(rects: Rect[]): Rect {
  const x = Math.min(...rects.map((r) => r.x));
  const y = Math.min(...rects.map((r) => r.y));
  const x2 = Math.max(...rects.map((r) => r.x + r.w));
  const y2 = Math.max(...rects.map((r) => r.y + r.h));
  return { x, y, w: x2 - x, h: y2 - y };
}

/**
 * One box per loop that holds at least one step: its own node plus its
 * members (a nested loop counts with its whole box), padded, with a header.
 * `exclude` leaves one step out, so the box under a dragged node shows where
 * the container is without that node stretching it.
 */
export function containerBoxes(
  steps: Step[],
  rectOf: (s: Step) => Rect,
  exclude: string | null = null,
  edges: Edge[] = [],
): ContainerBox[] {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const memo = new Map<string, ContainerBox | null>();
  const visiting = new Set<string>();

  const depthOf = (s: Step): number => {
    let d = 0;
    let cur = s.parent ? byId.get(s.parent) : undefined;
    const seen = new Set<string>();
    while (cur && !seen.has(cur.id)) {
      seen.add(cur.id);
      d++;
      cur = cur.parent ? byId.get(cur.parent) : undefined;
    }
    return d;
  };

  const boxOf = (loop: Step): ContainerBox | null => {
    if (memo.has(loop.id)) return memo.get(loop.id)!;
    if (visiting.has(loop.id)) return null; // parent cycle: draw nothing
    visiting.add(loop.id);
    const members = steps.filter(
      (s) => s.parent === loop.id && s.id !== exclude,
    );
    let box: ContainerBox | null = null;
    const wired = edges.some(
      (e) => e.source === loop.id && e.source_port === "loop",
    );
    const hasAny = steps.some((s) => s.parent === loop.id);
    if (!hasAny && !wired) {
      // Fresh loop: an empty zone to drop the first step into.
      const r = rectOf(loop);
      const u = union([
        r,
        { x: r.x + r.w + EMPTY_ZONE_GAP, y: r.y, w: EMPTY_ZONE_W, h: r.h },
      ]);
      box = {
        loopId: loop.id,
        depth: depthOf(loop),
        x: u.x - BOX_PAD,
        y: u.y - BOX_PAD - BOX_HEAD,
        w: u.w + 2 * BOX_PAD,
        h: u.h + 2 * BOX_PAD + BOX_HEAD,
        empty: true,
      };
    } else if (members.length) {
      const rects: Rect[] = [rectOf(loop)];
      for (const m of members) {
        const inner = m.kind === "loop" ? boxOf(m) : null;
        rects.push(inner ?? rectOf(m));
      }
      const u = union(rects);
      box = {
        loopId: loop.id,
        depth: depthOf(loop),
        x: u.x - BOX_PAD,
        y: u.y - BOX_PAD - BOX_HEAD,
        w: u.w + 2 * BOX_PAD,
        h: u.h + 2 * BOX_PAD + BOX_HEAD,
      };
    }
    visiting.delete(loop.id);
    memo.set(loop.id, box);
    return box;
  };

  return steps
    .filter((s) => s.kind === "loop")
    .map(boxOf)
    .filter((b): b is ContainerBox => b !== null)
    .sort((a, b) => a.depth - b.depth);
}

/** The innermost box containing the point, ignoring `skip` loops. */
export function innermostBoxAt(
  boxes: ContainerBox[],
  x: number,
  y: number,
  skip: Set<string> = new Set(),
): string | null {
  let best: ContainerBox | null = null;
  for (const b of boxes) {
    if (skip.has(b.loopId)) continue;
    if (x < b.x || x > b.x + b.w || y < b.y || y > b.y + b.h) continue;
    if (
      !best ||
      b.depth > best.depth ||
      (b.depth === best.depth && b.w * b.h < best.w * best.h)
    ) {
      best = b;
    }
  }
  return best?.loopId ?? null;
}

/** Slack around a box when deciding whether a dragged step stayed inside. */
export const KEEP_MARGIN = 24;

/**
 * The container a dragged step belongs to after the drop. Measured against
 * the boxes as they stood when the drag began (the step still counted in
 * its box), grown by `margin`, so a nudge inside a box never ejects it and
 * a box can be widened a little at a time.
 */
export function containerAfterDrag(
  boxesAtStart: ContainerBox[],
  cx: number,
  cy: number,
  skip: Set<string>,
  margin = KEEP_MARGIN,
): string | null {
  const grown = boxesAtStart.map((b) => ({
    ...b,
    x: b.x - margin,
    y: b.y - margin,
    w: b.w + 2 * margin,
    h: b.h + 2 * margin,
  }));
  return innermostBoxAt(grown, cx, cy, skip);
}

function outAdjacency(edges: Edge[]): Map<string, string[]> {
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    adj.set(e.source, [...(adj.get(e.source) ?? []), e.target]);
  }
  return adj;
}

/** Steps reachable from the loop's `loop` port without passing the loop. */
export function bodyChain(edges: Edge[], loopId: string): string[] {
  const entry = edges.find(
    (e) => e.source === loopId && e.source_port === "loop",
  )?.target;
  const adj = outAdjacency(edges);
  const seen: string[] = [];
  const todo = entry ? [entry] : [];
  while (todo.length) {
    const n = todo.pop()!;
    if (n === loopId || seen.includes(n)) continue;
    seen.push(n);
    todo.push(...(adj.get(n) ?? []));
  }
  return seen;
}

/** "Make container": adopt an old loop's body chain (members keep a parent
 *  they already have). */
export function adoptBody(
  steps: Step[],
  edges: Edge[],
  loopId: string,
): Step[] {
  const body = new Set(bodyChain(edges, loopId));
  return steps.map((s) =>
    body.has(s.id) && !s.parent ? { ...s, parent: loopId } : s,
  );
}

/** Rename keeps membership: children of `oldId` now point at `newId`. */
export function renameParent(
  steps: Step[],
  oldId: string,
  newId: string,
): Step[] {
  return steps.map((s) => (s.parent === oldId ? { ...s, parent: newId } : s));
}

/** Deleting a loop releases its direct children to the loop's own parent. */
export function releaseChildren(steps: Step[], loop: Step): Step[] {
  return steps.map((s) =>
    s.parent === loop.id ? { ...s, parent: loop.parent ?? null } : s,
  );
}

/** Why a wire may not be drawn across a container, or null when it may. */
export function crossingError(
  steps: Step[],
  from: string,
  port: string,
  to: string,
): string | null {
  for (const loop of steps.filter((s) => s.kind === "loop")) {
    const inner = descendantIds(steps, loop.id);
    if (!inner.size) continue;
    if (from === loop.id && port === "loop") {
      if (!inner.has(to))
        return `Loop ${loop.id} enters a step outside its box.`;
      continue;
    }
    if (inner.has(from) !== inner.has(to)) {
      return (
        `A wire can't cross the box of loop ${loop.id}. ` +
        "Leave a loop with On failure → Leave loop, or by ending the body."
      );
    }
  }
  return null;
}

/** The loop a step runs inside: its container, else an old loop whose body
 *  chain reaches it. */
export function enclosingLoop(
  steps: Step[],
  edges: Edge[],
  id: string,
): Step | null {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const step = byId.get(id);
  if (step?.parent) return byId.get(step.parent) ?? null;
  for (const loop of steps.filter((s) => s.kind === "loop")) {
    if (descendantIds(steps, loop.id).size) continue;
    if (bodyChain(edges, loop.id).includes(id)) return loop;
  }
  return null;
}

export interface EffectiveRoute {
  /** What a failure of this step does. */
  route: FailurePolicy | "error_port";
  /** Where that came from, for the "inherited from …" hint. */
  source: "error_port" | "step" | "loop" | "scenario";
  loopId?: string;
  /** Whether the failure counts as passed (handled_failures). */
  passes: boolean;
}

/** Same order as the runner: error port, step, nearest loop, scenario. */
export function effectiveRoute(
  steps: Step[],
  edges: Edge[],
  id: string,
  stopOnFailure: boolean,
  scenarioHandled: "pass" | "fail" = "pass",
): EffectiveRoute {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const step = byId.get(id);
  const loops: Step[] = [];
  let cur = enclosingLoop(steps, edges, id);
  const seen = new Set<string>();
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    loops.push(cur);
    cur = enclosingLoop(steps, edges, cur.id);
  }
  const handled = (): boolean => {
    for (const lp of loops) {
      const hf = lp.config?.handled_failures;
      if (hf === "pass" || hf === "fail") return hf === "pass";
    }
    return scenarioHandled === "pass";
  };
  if (edges.some((e) => e.source === id && e.source_port === "error")) {
    return { route: "error_port", source: "error_port", passes: handled() };
  }
  let route: FailurePolicy | null = step?.on_failure ?? null;
  let source: EffectiveRoute["source"] = "step";
  let loopId: string | undefined;
  if (!route) {
    for (const lp of loops) {
      if (lp.config?.on_failure) {
        route = lp.config.on_failure;
        source = "loop";
        loopId = lp.id;
        break;
      }
    }
  }
  if ((route === "next" || route === "break") && !loops.length) route = null;
  if (!route) {
    return {
      route: stopOnFailure ? "stop" : "ignore",
      source: "scenario",
      passes: false, // the legacy route is never "handled"
    };
  }
  return {
    route,
    source,
    loopId,
    passes: route !== "stop" && handled(),
  };
}
