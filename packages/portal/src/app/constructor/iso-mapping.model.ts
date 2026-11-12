/**
 * Pure model for the two ISO 8583 Message Format sections that are not the DE
 * table: the MTI list (`definition.mti`) and the per-MTI presence matrix
 * (`definition.presence`).
 *
 * - `definition.mti` is the MTI menu a step offers once this format is bound
 *   to it; the first entry is the MTI a step switches to when its current one
 *   is not in the list (constructor `applyDialect`).
 * - `definition.presence` is `{mti | "default": {mandatory: [de], conditional?:
 *   [de]}}`. A bound simulator (`payprobe_common.iso8583.validate_message`)
 *   and the step frame preview (worker `tcp/protocols.py` `check`, only when
 *   the step payload carries `presence`) look a rule up as
 *   `presence[mti] or presence["default"]`: an exact MTI rule REPLACES
 *   default, it does not merge with it. Only `mandatory` is enforced;
 *   `conditional` is recorded for people and is not validated by anything.
 */

export type Mark = "" | "M" | "C";

export const DEFAULT_RULE = "default";

export interface PresenceRule {
  /** An MTI ("0200") or "default". */
  key: string;
  mandatory: string[];
  conditional: string[];
  /** Keys of the stored rule the editor does not own, carried through a save. */
  extra: Record<string, unknown>;
}

export const MTI_RE = /^\d{4}$/;

const VERSION: Record<string, string> = {
  "0": "1987",
  "1": "1993",
  "2": "2003",
  "8": "national",
  "9": "private",
};
const CLASS: Record<string, string> = {
  "1": "Authorization",
  "2": "Financial",
  "3": "File action",
  "4": "Reversal / chargeback",
  "5": "Reconciliation",
  "6": "Administrative",
  "7": "Fee collection",
  "8": "Network management",
};
const FUNCTION: Record<string, string> = {
  "0": "request",
  "1": "request response",
  "2": "advice",
  "3": "advice response",
  "4": "notification",
  "5": "notification ack",
  "6": "instruction",
  "7": "instruction ack",
};
const ORIGIN: Record<string, string> = {
  "0": "acquirer",
  "1": "acquirer repeat",
  "2": "issuer",
  "3": "issuer repeat",
  "4": "other",
  "5": "other repeat",
};

/** "Authorization request" for 0100; "" for anything that is not 4 digits. */
export function mtiLabel(mti: string): string {
  if (!MTI_RE.test(mti)) return "";
  const cls = CLASS[mti[1]] ?? `class ${mti[1]}`;
  const fn = FUNCTION[mti[2]] ?? `function ${mti[2]}`;
  const origin = mti[3] === "0" ? "" : ` (${ORIGIN[mti[3]] ?? mti[3]})`;
  return `${cls} ${fn}${origin}`;
}

/** Full decode for a tooltip: version · class · function · origin. */
export function mtiTitle(mti: string): string {
  if (!MTI_RE.test(mti)) return mti;
  return [
    `ISO 8583:${VERSION[mti[0]] ?? mti[0]}`,
    CLASS[mti[1]] ?? `class ${mti[1]}`,
    FUNCTION[mti[2]] ?? `function ${mti[2]}`,
    `from ${ORIGIN[mti[3]] ?? mti[3]}`,
  ].join(" · ");
}

/** MTIs offered in the add box (picked, not typed). Any 4-digit MTI is legal. */
export const COMMON_MTIS: string[] = [
  "0100",
  "0110",
  "0120",
  "0121",
  "0130",
  "0200",
  "0210",
  "0220",
  "0221",
  "0230",
  "0400",
  "0410",
  "0420",
  "0421",
  "0430",
  "0500",
  "0510",
  "0520",
  "0530",
  "0600",
  "0610",
  "0620",
  "0630",
  "0800",
  "0810",
  "0820",
  "0830",
  "1100",
  "1110",
  "1120",
  "1130",
  "1200",
  "1210",
  "1220",
  "1230",
  "1420",
  "1421",
  "1430",
  "1500",
  "1510",
  "1520",
  "1530",
  "1604",
  "1614",
  "1804",
  "1814",
  "1820",
  "1830",
];

/** DE numbers as strings, de-duplicated, sorted numerically. */
export function sortDes(list: Iterable<unknown>): string[] {
  const set = new Set<string>();
  for (const d of list) {
    const s = String(d ?? "").trim();
    if (s) set.add(s);
  }
  return [...set].sort((a, b) => Number(a) - Number(b) || a.localeCompare(b));
}

/** `definition.mti` as a clean list (strings, de-duplicated, order kept). */
export function parseMtis(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const m of raw) {
    const s = String(m ?? "").trim();
    if (s && !out.includes(s)) out.push(s);
  }
  return out;
}

/**
 * `definition.presence` as editable rules. An empty `{}` rule is dropped: both
 * validators treat it as falsy and fall back to default, so dropping it keeps
 * the behaviour and the screen honest.
 */
export function parsePresence(raw: unknown): PresenceRule[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  const rules: PresenceRule[] = [];
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const v =
      value && typeof value === "object" && !Array.isArray(value)
        ? (value as Record<string, unknown>)
        : {};
    const { mandatory, conditional, ...extra } = v;
    if (!Object.keys(v).length) continue;
    rules.push({
      key,
      mandatory: sortDes(Array.isArray(mandatory) ? mandatory : []),
      conditional: sortDes(Array.isArray(conditional) ? conditional : []),
      extra,
    });
  }
  return sortRules(rules);
}

/** MTIs ascending, default last. */
export function sortRules(rules: PresenceRule[]): PresenceRule[] {
  return [...rules].sort((a, b) =>
    a.key === DEFAULT_RULE
      ? 1
      : b.key === DEFAULT_RULE
        ? -1
        : a.key.localeCompare(b.key),
  );
}

/**
 * Rules back to `definition.presence`, or undefined when there are none (the
 * orchestrator snapshots presence only when the key is truthy). A rule always
 * writes its `mandatory` list, even empty: `{"mandatory": []}` is the explicit
 * "nothing required for this MTI" that stops the default from applying.
 */
export function buildPresence(
  rules: PresenceRule[],
): Record<string, unknown> | undefined {
  if (!rules.length) return undefined;
  const out: Record<string, unknown> = {};
  for (const r of sortRules(rules)) {
    const rule: Record<string, unknown> = {
      ...r.extra,
      mandatory: sortDes(r.mandatory),
    };
    if (r.conditional.length) rule["conditional"] = sortDes(r.conditional);
    out[r.key] = rule;
  }
  return out;
}

export function markOf(rule: PresenceRule | undefined, de: string): Mark {
  if (!rule) return "";
  if (rule.mandatory.includes(de)) return "M";
  if (rule.conditional.includes(de)) return "C";
  return "";
}

/** Set one DE's mark in a rule, in place. */
export function setMark(rule: PresenceRule, de: string, mark: Mark): void {
  rule.mandatory = rule.mandatory.filter((d) => d !== de);
  rule.conditional = rule.conditional.filter((d) => d !== de);
  if (mark === "M") rule.mandatory = sortDes([...rule.mandatory, de]);
  if (mark === "C") rule.conditional = sortDes([...rule.conditional, de]);
}

/** Cell click order: none → mandatory → conditional → none. */
export function nextMark(mark: Mark): Mark {
  return mark === "" ? "M" : mark === "M" ? "C" : "";
}

/** A rule that starts as a copy of another (or empty). */
export function copyRule(key: string, from?: PresenceRule): PresenceRule {
  return {
    key,
    mandatory: [...(from?.mandatory ?? [])],
    conditional: [...(from?.conditional ?? [])],
    extra: {},
  };
}
