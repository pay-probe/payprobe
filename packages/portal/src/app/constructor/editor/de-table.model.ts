/** A step's DE table override (`payload.fields`) as editable rows: pure
 *  functions for the DE table editor in the step inspector. No Angular here.
 *
 *  Binding a dialect copies its whole table into `payload.fields`, so this map
 *  is usually the dialect's table plus whatever the author changed for this
 *  step. Rows are compared with a *baseline* (the bound dialect's table, else
 *  the stock ISO 8583:1987 one) so only the differences need attention.
 *  Keys the form has no field for (a per-field `encoding`, …) are kept. */

import { DeSpec, DeTable, formatHint } from "./de-values.model";

/** Length types the codec frames (`codec.LEN_PREFIX` plus fixed). */
export const LEN_TYPES = [
  "fixed",
  "llvar",
  "lllvar",
  "llllvar",
  "lllllvar",
] as const;

/** Field classes the codec knows (see `payprobe_common/iso8583/fields.py`). */
export const FIELD_TYPES: { value: string; label: string }[] = [
  { value: "n", label: "n · numeric" },
  { value: "a", label: "a · alpha" },
  { value: "an", label: "an · alphanumeric" },
  { value: "ans", label: "ans · alphanumeric + special" },
  { value: "z", label: "z · track data" },
  { value: "b", label: "b · binary (hex)" },
];

export interface TableRow {
  de: string;
  spec: DeSpec & Record<string, unknown>;
}

export type RowState = "same" | "changed" | "added";

function deNum(de: string): number {
  const n = Number(de);
  return Number.isFinite(n) ? n : Number.MAX_SAFE_INTEGER;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}

/** Parse a stored `fields` (object, or a JSON string from older saves) into
 *  rows sorted by DE; null when it is not a JSON object. */
export function tableRowsFromValue(raw: unknown): TableRow[] | null {
  let obj: unknown = raw;
  if (raw === undefined || raw === null || raw === "") return [];
  if (typeof raw === "string") {
    try {
      obj = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!isObject(obj)) return null;
  return Object.entries(obj)
    .map(([de, spec]) => ({
      de: String(de),
      spec: (isObject(spec) ? { ...spec } : {}) as TableRow["spec"],
    }))
    .sort((a, b) => deNum(a.de) - deNum(b.de) || a.de.localeCompare(b.de));
}

/** Rows back to the stored map, in DE order. Blank DEs are dropped, and so
 *  are empty `name`s (the codec does not need one). */
export function tableFromRows(rows: TableRow[]): DeTable {
  const out: DeTable = {};
  const sorted = [...rows].sort(
    (a, b) => deNum(a.de) - deNum(b.de) || a.de.localeCompare(b.de),
  );
  for (const r of sorted) {
    const de = r.de.trim();
    if (!de) continue;
    const spec: Record<string, unknown> = { ...r.spec };
    if (spec["name"] === "") delete spec["name"];
    out[de] = spec as DeSpec;
  }
  return out;
}

/** Whether a row matches its baseline entry. Only the codec-relevant keys
 *  (len_type, length, type, plus any extra key such as `encoding`) count;
 *  a renamed field is still "same" on the wire, so `name` is ignored. */
export function rowState(row: TableRow, baseline: DeTable): RowState {
  const base = baseline[row.de];
  if (!base) return "added";
  const keys = new Set([...Object.keys(row.spec), ...Object.keys(base)]);
  keys.delete("name");
  for (const k of keys) {
    const a = (row.spec as Record<string, unknown>)[k];
    const b = (base as Record<string, unknown>)[k];
    if (JSON.stringify(a ?? null) !== JSON.stringify(b ?? null))
      return "changed";
  }
  return "same";
}

/** "was n 12 fixed": how the baseline defines a changed DE. */
export function baselineHint(de: string, baseline: DeTable): string {
  const b = baseline[de];
  return b ? formatHint(b) : "";
}

/** Why a row would be refused by the codec; null when it is fine. */
export function validateTableRow(row: TableRow): string | null {
  const n = Number(row.de);
  if (!/^\d+$/.test(row.de) || n < 2 || n > 192)
    return `DE number must be 2 to 192, got "${row.de}"`;
  const lt = row.spec.len_type ?? "fixed";
  if (!(LEN_TYPES as readonly string[]).includes(lt))
    return `DE${row.de}: length type must be one of ${LEN_TYPES.join(", ")}`;
  const len = row.spec.length;
  if (len === undefined || len === null || !Number.isInteger(len) || len < 0)
    return `DE${row.de}: length must be a whole number`;
  if (lt === "fixed" && len === 0)
    return `DE${row.de}: a fixed field needs a length`;
  const max = { llvar: 99, lllvar: 999, llllvar: 9999, lllllvar: 99999 }[lt];
  if (max !== undefined && len > max)
    return `DE${row.de}: ${lt} carries at most ${max}, got ${len}`;
  return null;
}

/** A row for a DE added by hand: the baseline's definition when it has one,
 *  else a blank fixed `ans` field to fill in. */
export function newRow(de: string, baseline: DeTable): TableRow {
  const base = baseline[de];
  return {
    de,
    spec: base
      ? ({ ...base } as TableRow["spec"])
      : { name: "", len_type: "fixed", length: 0, type: "ans" },
  };
}

/** Rows to show: optionally only the ones that differ from the baseline,
 *  filtered by DE number prefix or name substring. */
export function visibleRows(
  rows: TableRow[],
  baseline: DeTable,
  query: string,
  changedOnly: boolean,
): TableRow[] {
  const q = query
    .trim()
    .toLowerCase()
    .replace(/^de\s*/, "");
  return rows.filter(
    (r) =>
      (!changedOnly || rowState(r, baseline) !== "same") &&
      (!q ||
        r.de.startsWith(q) ||
        String(r.spec.name ?? "")
          .toLowerCase()
          .includes(q)),
  );
}
