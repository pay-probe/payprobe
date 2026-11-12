import { Edge, Step } from "../scenario.models";
import {
  BOX_HEAD,
  BOX_PAD,
  containerAfterDrag,
  adoptBody,
  containerBoxes,
  crossingError,
  effectiveRoute,
  innermostBoxAt,
  releaseChildren,
  renameParent,
} from "./loop-container.model";

const step = (id: string, extra: Partial<Step> = {}): Step => ({
  id,
  kind: "action",
  target: "http",
  action: "echo_test",
  payload: {},
  assertions: [],
  ...extra,
});
const loop = (id: string, extra: Partial<Step> = {}): Step =>
  step(id, { kind: "loop", target: "", action: "", config: {}, ...extra });

const POS: Record<string, { x: number; y: number }> = {
  lp: { x: 0, y: 0 },
  a: { x: 300, y: 0 },
  b: { x: 600, y: 100 },
  inner: { x: 300, y: 300 },
  c: { x: 600, y: 300 },
};
const rectOf = (s: Step) => ({ ...POS[s.id], w: 200, h: 80 });

describe("loop container model", () => {
  it("boxes a loop with its members, padded, with a header", () => {
    const steps = [
      loop("lp"),
      step("a", { parent: "lp" }),
      step("b", { parent: "lp" }),
    ];
    const [box] = containerBoxes(steps, rectOf);
    expect(box.loopId).toBe("lp");
    expect(box.x).toBe(-BOX_PAD);
    expect(box.y).toBe(-BOX_PAD - BOX_HEAD);
    expect(box.w).toBe(800 + 2 * BOX_PAD);
    expect(box.h).toBe(180 + 2 * BOX_PAD + BOX_HEAD);
  });

  it("gives a fresh loop an empty drop zone, an old wired loop none", () => {
    const [zone] = containerBoxes([loop("lp")], rectOf);
    expect(zone.empty).toBe(true);
    expect(innermostBoxAt([zone], 400, 40)).toBe("lp");
    const wired: Edge[] = [{ source: "lp", source_port: "loop", target: "a" }];
    expect(
      containerBoxes([loop("lp"), step("a")], rectOf, null, wired),
    ).toEqual([]);
  });

  it("nests inner boxes inside outer ones", () => {
    const steps = [
      loop("lp"),
      loop("inner", { parent: "lp" }),
      step("c", { parent: "inner" }),
    ];
    const boxes = containerBoxes(steps, rectOf);
    expect(boxes.map((b) => b.loopId)).toEqual(["lp", "inner"]);
    const [outer, inner] = boxes;
    expect(inner.x).toBeGreaterThan(outer.x);
    expect(inner.y + inner.h).toBeLessThan(outer.y + outer.h);
    expect(innermostBoxAt(boxes, 650, 320)).toBe("inner");
    expect(innermostBoxAt(boxes, 50, 40)).toBe("lp");
    expect(innermostBoxAt(boxes, 5000, 5000)).toBe(null);
  });

  it("leaves the dragged member out so the box does not follow it", () => {
    const steps = [
      loop("lp"),
      step("a", { parent: "lp" }),
      step("b", { parent: "lp" }),
    ];
    const [box] = containerBoxes(steps, rectOf, "b");
    expect(box.w).toBe(500 + 2 * BOX_PAD);
  });

  it("refuses wires across a box, except the loop's own entry", () => {
    const steps = [loop("lp"), step("a", { parent: "lp" }), step("after")];
    expect(crossingError(steps, "lp", "loop", "a")).toBe(null);
    expect(crossingError(steps, "lp", "done", "after")).toBe(null);
    expect(crossingError(steps, "a", "out", "after")).toContain("can't cross");
    expect(crossingError(steps, "a", "error", "after")).toContain(
      "can't cross",
    );
    expect(crossingError(steps, "lp", "loop", "after")).toContain(
      "outside its box",
    );
  });

  it("adopts an old body, keeps membership on rename, releases on delete", () => {
    const edges: Edge[] = [
      { source: "lp", source_port: "loop", target: "a" },
      { source: "a", source_port: "out", target: "b" },
      { source: "lp", source_port: "done", target: "c" },
    ];
    const adopted = adoptBody(
      [loop("lp"), step("a"), step("b"), step("c")],
      edges,
      "lp",
    );
    expect(adopted.filter((s) => s.parent === "lp").map((s) => s.id)).toEqual([
      "a",
      "b",
    ]);
    const renamed = renameParent(adopted, "lp", "each_card");
    expect(renamed.filter((s) => s.parent === "each_card").length).toBe(2);
    const released = releaseChildren(renamed, loop("each_card"));
    expect(released.every((s) => !s.parent)).toBe(true);
  });

  it("resolves the route like the runner: error port, step, loop, scenario", () => {
    const edges: Edge[] = [{ source: "lp", source_port: "loop", target: "a" }];
    const lp = loop("lp", { config: { on_failure: "next" } });
    const steps = [lp, step("a", { parent: "lp" }), step("solo")];
    expect(effectiveRoute(steps, edges, "a", true)).toEqual({
      route: "next",
      source: "loop",
      loopId: "lp",
      passes: true,
    });
    const own = [lp, step("a", { parent: "lp", on_failure: "ignore" })];
    expect(effectiveRoute(own, edges, "a", true).route).toBe("ignore");
    const wired = [
      ...edges,
      { source: "a", source_port: "error", target: "a2" },
    ];
    expect(effectiveRoute(steps, wired, "a", true).route).toBe("error_port");
    expect(effectiveRoute(steps, edges, "solo", true)).toEqual({
      route: "stop",
      source: "scenario",
      passes: false,
    });
    const failing = [
      loop("lp", { config: { on_failure: "next", handled_failures: "fail" } }),
      step("a", { parent: "lp" }),
    ];
    expect(effectiveRoute(failing, edges, "a", true).passes).toBe(false);
  });

  it("keeps a nudged member in its box and lets a far drag take it out", () => {
    // The loop's only member: at drag start the box still contains it.
    const steps = [loop("lp"), step("a", { parent: "lp" })];
    const atStart = containerBoxes(steps, rectOf);
    const skip = new Set(["a"]);
    // nudged 30 px right of its old centre (400, 40): still inside
    expect(containerAfterDrag(atStart, 430, 40, skip)).toBe("lp");
    // a little past the right edge (within the margin): still inside
    const edge = atStart[0].x + atStart[0].w;
    expect(containerAfterDrag(atStart, edge + 10, 40, skip)).toBe("lp");
    // dragged well away: out of the loop
    expect(containerAfterDrag(atStart, edge + 200, 40, skip)).toBe(null);
  });
});
