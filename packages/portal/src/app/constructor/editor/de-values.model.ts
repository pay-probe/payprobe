/** ISO 8583 field values (`payload.values`) as editable rows: pure functions
 *  for the DE editor in the scenario step inspector. No Angular here.
 *
 *  A stored `values` may be an object (`{"11": "000102"}`) or, from older
 *  saves, the same map as a JSON *string*: the generic JSON param editor kept
 *  the raw text whenever it contained a `${...}` reference. The worker accepts
 *  both (`json.loads` on a string); this editor reads both and always writes
 *  an object. */

/** One entry of a DE table (the Message Format / dialect `fields` shape,
 *  mirror of `payprobe_common/iso8583/fields.py`). */
export interface DeSpec {
  name?: string;
  len_type?: string;
  length?: number;
  type?: string;
}

export type DeTable = Record<string, DeSpec>;

export interface DeRow {
  de: string;
  value: string;
}

/** Per-transaction value generators the worker resolves in-process (mirror of
 *  `worker/engine/generators.py`): offered by the payload autocomplete so an
 *  author finds them without a Code step. `${seq.NAME(width)}` is a padded
 *  counter, unique across the load fleet; the rest are random / time / pool
 *  values. */
export const GENERATOR_REFS: readonly string[] = [
  "${seq.stan(6)}",
  "${seq.stan}",
  "${seq.ksn}",
  "${uuid}",
  "${now.rrn}",
  "${now.epoch}",
  "${now.iso}",
  "${now.fmt(%m%d%H%M%S)}",
  "${now.fmt(%H%M%S,4)}",
  "${rand.int(1,100)}",
  "${rand.amount(100,5000)}",
  "${rand.digits(4)}",
  "${rand.hex(8)}",
  "${rand.pan(411111)}",
  "${pool.terminal}",
  "${pool.card}",
  "${card.pan}",
  "${card.expiry}",
  "${card.cvv}",
  "${card.track2}",
  "${terminal.id}",
  "${terminal.merchant}",
  "${terminal.currency}",
];

/** One-click values per DE: a generator that makes the field vary per
 *  transaction, so a load run does not replay one message verbatim. */
export const DE_SUGGESTIONS: Record<
  string,
  { value: string; label: string }[]
> = {
  "2": [{ value: "${card.pan}", label: "card from pool" }],
  "4": [{ value: "${rand.amount(100,5000)}", label: "random amount" }],
  "7": [{ value: "${now.fmt(%m%d%H%M%S)}", label: "now (UTC)" }],
  "11": [{ value: "${seq.stan(6)}", label: "counter 000001…" }],
  "12": [{ value: "${now.fmt(%H%M%S)}", label: "now (UTC)" }],
  "13": [{ value: "${now.fmt(%m%d)}", label: "today (UTC)" }],
  "14": [{ value: "${card.expiry}", label: "card expiry" }],
  "35": [{ value: "${card.track2}", label: "card track 2" }],
  "37": [{ value: "${now.rrn}", label: "time-based RRN" }],
  "41": [{ value: "${terminal.id}", label: "terminal from pool" }],
  "42": [{ value: "${terminal.merchant}", label: "terminal's merchant" }],
  "49": [{ value: "${terminal.currency}", label: "terminal's currency" }],
};

/** DEs the connection fills by itself when the step leaves them out
 *  (`correlation.auto_generate` for DE 11, `auto_fields` for 7/12/13). */
export const AUTO_FILLED_DES = ["7", "11", "12", "13"] as const;

/** True when a value is (or contains) a `${...}` reference, resolved at run
 *  time and therefore not checkable here. */
export function isReference(value: string): boolean {
  return value.includes("${");
}

function deNum(de: string): number {
  const n = Number(de);
  return Number.isFinite(n) ? n : Number.MAX_SAFE_INTEGER;
}

/** Rows sorted by DE number (non-numeric keys last, alphabetically). */
export function sortRows(rows: DeRow[]): DeRow[] {
  return [...rows].sort(
    (a, b) => deNum(a.de) - deNum(b.de) || a.de.localeCompare(b.de),
  );
}

/** Parse a stored `values` (object or JSON string) into rows. Returns null
 *  when it is a string that is not a JSON object (the editor then falls back
 *  to the raw JSON view so nothing is lost). */
export function rowsFromValue(raw: unknown): DeRow[] | null {
  let obj: unknown = raw;
  if (raw === undefined || raw === null || raw === "") return [];
  if (typeof raw === "string") {
    try {
      obj = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
  return sortRows(
    Object.entries(obj as Record<string, unknown>).map(([de, v]) => ({
      de: String(de),
      value:
        v !== null && typeof v === "object"
          ? JSON.stringify(v)
          : String(v ?? ""),
    })),
  );
}

/** Rows back to the stored object, in DE order. Rows without a DE are
 *  dropped; a duplicate DE keeps the last value. */
export function valueFromRows(rows: DeRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of sortRows(rows)) {
    const de = r.de.trim();
    if (de) out[de] = r.value;
  }
  return out;
}

/** "n 6 fixed", "n ≤ 19 llvar": the shape of a DE in a few characters. */
export function formatHint(spec: DeSpec | undefined): string {
  if (!spec) return "";
  const type = spec.type ?? "";
  const lt = spec.len_type ?? "fixed";
  const len = spec.length ?? 0;
  if (lt === "fixed") return `${type} ${len} fixed`.trim();
  return `${type} ≤ ${len} ${lt}`.trim();
}

/** Why a literal value would not pack under its DE spec, in the codec's own
 *  wording ("DE4 expected 12 chars, got 11"); null when it fits, when it is a
 *  reference, or when the DE is not in the table (see {@link unknownDe}). */
export function validateRow(
  row: DeRow,
  spec: DeSpec | undefined,
): string | null {
  const v = row.value;
  if (!spec || isReference(v)) return null;
  if (v === "") return `DE${row.de} is empty`;
  const len = spec.length ?? 0;
  const lt = spec.len_type ?? "fixed";
  const type = spec.type ?? "";
  if (type === "n" && !/^\d*$/.test(v))
    return `DE${row.de} is numeric (n): digits only`;
  if (type === "b" && !/^[0-9a-fA-F]*$/.test(v))
    return `DE${row.de} is binary (b): hex digits only`;
  if (len > 0) {
    if (lt === "fixed" && v.length !== len)
      return `DE${row.de} expected ${len} chars, got ${v.length}`;
    if (lt !== "fixed" && v.length > len)
      return `DE${row.de} allows at most ${len} chars, got ${v.length}`;
  }
  return null;
}

/** True when the DE is not in the active table (the codec would refuse it:
 *  "DE24 not defined in the field table"). */
export function unknownDe(de: string, table: DeTable): boolean {
  return Object.keys(table).length > 0 && !(de in table);
}

/** A literal timestamp in an auto-stamped time DE: sent unchanged on every
 *  transaction, which a host may reject as stale. */
export function isStaticTime(row: DeRow): boolean {
  return (
    (row.de === "7" || row.de === "12" || row.de === "13") &&
    row.value !== "" &&
    !isReference(row.value)
  );
}

/** A literal STAN: every transaction of a load run repeats it. */
export function isStaticStan(row: DeRow): boolean {
  return row.de === "11" && row.value !== "" && !isReference(row.value);
}

/** DEs of the table matching a search (number prefix or name substring),
 *  excluding the ones already present, in DE order. */
export function searchDes(
  table: DeTable,
  query: string,
  present: Iterable<string>,
  limit = 30,
): { de: string; spec: DeSpec }[] {
  const have = new Set(present);
  const q = query
    .trim()
    .toLowerCase()
    .replace(/^de\s*/, "");
  return Object.entries(table)
    .filter(([de]) => !have.has(de))
    .filter(
      ([de, spec]) =>
        !q || de.startsWith(q) || (spec.name ?? "").toLowerCase().includes(q),
    )
    .sort(([a], [b]) => deNum(a) - deNum(b))
    .slice(0, limit)
    .map(([de, spec]) => ({ de, spec }));
}

/** The DE table the step will be packed with: the step's own snapshot
 *  (`payload.fields`, written when a dialect is bound, and the table the wire
 *  uses), else the bound dialect's, else the stock ISO 8583:1987 one. */
export function activeTable(
  stepFields: unknown,
  dialectFields: unknown,
  stockFields: unknown,
): { table: DeTable; source: "step" | "dialect" | "stock" | "none" } {
  const asTable = (v: unknown): DeTable | null =>
    v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length
      ? (v as DeTable)
      : null;
  const s = asTable(stepFields);
  if (s) return { table: s, source: "step" };
  const d = asTable(dialectFields);
  if (d) return { table: d, source: "dialect" };
  const k = asTable(stockFields);
  if (k) return { table: k, source: "stock" };
  return { table: {}, source: "none" };
}
