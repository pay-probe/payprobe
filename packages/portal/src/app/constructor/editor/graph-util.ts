/**
 * Pure graph/geometry helpers extracted from the (large) constructor component.
 *
 * Everything here is stateless — inputs in, values out, no component `this` —
 * so it's independently readable and unit-testable, and shrinks the editor
 * component toward presentational + orchestration concerns only.
 */
import {
  Edge,
  NodeConfig,
  NodeKind,
  NodePosition,
  Step,
} from "../scenario.models";

// canvas layout constants
export const GRID_SNAP = 8;
export const AUTO_X = 80;
export const AUTO_Y = 240;
export const AUTO_GAP = 300;

/** Linear edges connecting a list of steps in order (legacy linear scenarios). */
export function sequentialEdges(steps: Step[]): Edge[] {
  const edges: Edge[] = [];
  for (let i = 0; i < steps.length - 1; i++) {
    edges.push({
      source: steps[i].id,
      source_port: "out",
      target: steps[i + 1].id,
    });
  }
  return edges;
}

/** Auto-layout position for the node at ``index`` along the default row. */
export function autoPos(index: number): NodePosition {
  return { x: AUTO_X + index * AUTO_GAP, y: AUTO_Y };
}

/** Snap a coordinate to the canvas grid. */
export function snap(v: number): number {
  return Math.round(v / GRID_SNAP) * GRID_SNAP;
}

/** SVG cubic-bezier path between two ports (horizontal control handles). */
export function bezier(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

/** Rewrite ``${local.…}`` references to ``${real.…}`` when inserting a flow
 *  template (its placeholder node ids are remapped to real node ids). */
export function rewriteFlowRefs(
  value: string,
  refMap: Record<string, string>,
): string {
  let out = value;
  for (const [local, real] of Object.entries(refMap)) {
    out = out.split("${" + local + ".").join("${" + real + ".");
  }
  return out;
}

/** Default config for a freshly-dropped control/code/http node. */
export function defaultConfig(kind: Exclude<NodeKind, "action">): NodeConfig {
  switch (kind) {
    case "if":
      return { left: "", operator: "eq", right: "" };
    case "switch":
      return { value: "", cases: [{ value: "" }] };
    case "loop":
      return { mode: "times", count: 1, max_iterations: 1000 };
    case "wait":
      return { ms: 1000 };
    case "merge":
      return {};
    case "init":
      return { data: '{\n  "amount": 10000,\n  "currency": "USD"\n}' };
    case "crypto":
      return {
        operation: "pin_block_encode",
        key: "",
        pin: "",
        pan: "",
        cipher_mode: "ecb",
        cipher_op: "encrypt",
      };
    case "call":
      return { scenario_id: "", variables: [] };
    case "code":
      return {
        language: "python",
        timeout_ms: 5000,
        inputs: [],
        code:
          "# `context` holds earlier results, e.g. context['step_001']['response']\n" +
          "# Return a dict — it becomes ${" +
          "this_node.response.*} downstream.\n" +
          "return {}",
      };
    case "trigger":
      return {};
    case "reply":
      return {};
    case "state":
      return {};
    case "relay":
      return { relay_mode: "decode" };
    case "http":
      return {
        method: "GET",
        url: "",
        authentication: { type: "none" },
        sendQueryParameters: false,
        queryParameters: [],
        sendHeaders: false,
        headers: [],
        sendBody: false,
        contentType: "json",
        body: "",
        bodyParameters: [],
        options: {
          timeout_ms: 30000,
          followRedirects: true,
          ignoreSsl: false,
          responseFormat: "auto",
        },
      };
  }
}
