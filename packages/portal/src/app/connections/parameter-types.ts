/** Typed override parameters for the connection / environment override editors.
 *
 * A per-environment override is stored on the wire as a plain ``{key: value}``
 * map — the value is just a string / number / boolean. To make the editor less
 * error-prone we let the user pick a *type* per parameter and, for the two
 * reference types, choose the value from a dropdown:
 *
 *   string | number | boolean   — scalar values (text / number / true-false)
 *   adapter                      — one of the adapter kinds (tcp, grpc, http, …)
 *   connection                   — a registered connection name
 *
 * The type is an editing aid only — it is NOT persisted. On reload it is
 * re-derived from the stored value (see {@link inferParamType}), so nothing in
 * the storage shape changes.
 */

export type ParamType =
  | "string"
  | "number"
  | "boolean"
  | "adapter"
  | "connection";

/** One editable override row. ``type`` drives which value control is shown and
 * how the value is coerced on save; it is not stored. */
export interface ParamRow {
  key: string;
  value: string;
  type: ParamType;
}

/** The adapter implementations a parameter of type ``adapter`` can reference.
 * Matches the worker AdapterRegistry / connection allowlist. */
export const ADAPTER_KINDS: readonly string[] = [
  "tcp",
  "grpc",
  "http",
  "nats",
  "payshield",
  "db_probe_core",
  "db_probe_switch",
];

export const PARAM_TYPES: readonly ParamType[] = [
  "string",
  "number",
  "boolean",
  "adapter",
  "connection",
];

/** Compact labels for the type dropdown, shared by every parameter editor. */
export const PARAM_TYPE_LABELS: Record<ParamType, string> = {
  string: "str",
  number: "num",
  boolean: "bool",
  adapter: "adapter",
  connection: "connection",
};

function isNumeric(s: string): boolean {
  const t = s.trim();
  return t !== "" && !/[^0-9.+-]/.test(t) && Number.isFinite(Number(t));
}

/** Re-derive a parameter's type from its stored value. ``connectionNames`` lets
 * a value that matches a known connection resolve to the ``connection`` type;
 * otherwise a value matching an adapter kind resolves to ``adapter``. Order:
 * boolean → number → connection → adapter → string. */
export function inferParamType(
  value: string,
  connectionNames: readonly string[] = [],
): ParamType {
  const v = String(value);
  if (v === "true" || v === "false") return "boolean";
  if (isNumeric(v)) return "number";
  if (connectionNames.includes(v)) return "connection";
  if (ADAPTER_KINDS.includes(v)) return "adapter";
  return "string";
}

/** Coerce an edited string value into the worker-shaped value for its type.
 * adapter / connection stay strings (the kind / the name). */
export function coerceParamValue(value: string, type: ParamType): unknown {
  const t = value.trim();
  switch (type) {
    case "number": {
      const n = Number(t);
      return Number.isFinite(n) ? n : value;
    }
    case "boolean":
      return t === "true";
    default:
      return value;
  }
}
