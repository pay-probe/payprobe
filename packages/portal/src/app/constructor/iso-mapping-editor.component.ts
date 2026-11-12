import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
  signal,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import {
  COMMON_MTIS,
  DEFAULT_RULE,
  Mark,
  MTI_RE,
  PresenceRule,
  copyRule,
  markOf,
  mtiLabel,
  mtiTitle,
  nextMark,
  setMark,
  sortDes,
} from "./iso-mapping.model";

/** The part of an ISO 8583 format draft this editor owns, mutated in place. */
export interface IsoMapping {
  mtis: string[];
  rules: PresenceRule[];
}

export interface MappingColumn {
  key: string;
  label: string;
  title: string;
  inList: boolean;
  rule: PresenceRule | undefined;
}

export interface MappingRow {
  de: string;
  name: string;
  inTable: boolean;
}

/**
 * MTI list and field presence mapping of an ISO 8583 Message Format: which
 * MTIs a bound step offers (`definition.mti`) and which DEs each MTI must
 * carry (`definition.presence`). Edits the draft in place and emits `changed`
 * after every real edit; the filter controls never mark the format dirty.
 */
@Component({
  selector: "app-iso-mapping-editor",
  standalone: true,
  imports: [FormsModule],
  templateUrl: "./iso-mapping-editor.component.html",
  styleUrl: "./iso-mapping-editor.component.scss",
  changeDetection: ChangeDetectionStrategy.Eager,
})
export class IsoMappingEditorComponent {
  readonly model = input.required<IsoMapping>();
  /** The format's DE table rows (number + name), in any order. */
  readonly des = input<{ de: string; name: string }[]>([]);
  readonly readonly = input(false);
  readonly changed = output<void>();

  readonly mtiOpen = signal(true);
  readonly gridOpen = signal(true);
  readonly newMti = signal("");
  readonly mtiError = signal<string | null>(null);
  /** null = automatic: only marked DEs once any DE is marked. */
  readonly onlyMarked = signal<boolean | null>(null);
  readonly query = signal("");

  readonly DEFAULT = DEFAULT_RULE;
  readonly mtiLabel = mtiLabel;
  readonly mtiTitle = mtiTitle;

  // -- MTI list --------------------------------------------------------------

  suggestions(): string[] {
    const have = new Set(this.model().mtis);
    return COMMON_MTIS.filter((m) => !have.has(m));
  }

  addMti(): void {
    const mti = this.newMti().trim();
    const m = this.model();
    if (!MTI_RE.test(mti)) {
      this.mtiError.set("An MTI is 4 digits, e.g. 0200.");
      return;
    }
    if (m.mtis.includes(mti)) {
      this.mtiError.set(`${mti} is already in the list.`);
      return;
    }
    m.mtis.push(mti);
    this.newMti.set("");
    this.mtiError.set(null);
    this.changed.emit();
  }

  removeMti(i: number): void {
    this.model().mtis.splice(i, 1);
    this.changed.emit();
  }

  /** Move one place earlier; index 0 is the MTI a bound step falls back to. */
  moveEarlier(i: number): void {
    const list = this.model().mtis;
    if (i <= 0) return;
    [list[i - 1], list[i]] = [list[i], list[i - 1]];
    this.changed.emit();
  }

  // -- presence mapping ------------------------------------------------------

  rule(key: string): PresenceRule | undefined {
    return this.model().rules.find((r) => r.key === key);
  }

  /** default first, then the MTI list in order, then rules for MTIs that are
   *  not in the list (still applied to inbound messages by a simulator). */
  columns(): MappingColumn[] {
    const m = this.model();
    const keys = [DEFAULT_RULE, ...m.mtis];
    for (const r of m.rules) if (!keys.includes(r.key)) keys.push(r.key);
    return keys.map((key) => ({
      key,
      label: key === DEFAULT_RULE ? "any other MTI" : mtiLabel(key),
      title:
        key === DEFAULT_RULE
          ? "Applies to every MTI that has no rule of its own"
          : mtiTitle(key),
      // with no MTI list a step offers the stock menu: no rule is an orphan
      inList: key === DEFAULT_RULE || !m.mtis.length || m.mtis.includes(key),
      rule: this.rule(key),
    }));
  }

  markedDes(): Set<string> {
    const s = new Set<string>();
    for (const r of this.model().rules) {
      r.mandatory.forEach((d) => s.add(d));
      r.conditional.forEach((d) => s.add(d));
    }
    return s;
  }

  onlyMarkedEffective(): boolean {
    return this.onlyMarked() ?? this.markedDes().size > 0;
  }

  /** DE table rows plus any marked DE the table lacks, filtered. */
  rows(): MappingRow[] {
    const names = new Map<string, string>();
    for (const r of this.des()) {
      const de = String(r.de ?? "").trim();
      if (de) names.set(de, r.name);
    }
    const marked = this.markedDes();
    const q = this.query().trim().toLowerCase();
    const only = this.onlyMarkedEffective();
    return sortDes([...names.keys(), ...marked])
      .map((de) => ({ de, name: names.get(de) ?? "", inTable: names.has(de) }))
      .filter((r) => !only || marked.has(r.de))
      .filter((r) => !q || r.de === q || r.name.toLowerCase().includes(q));
  }

  totalRows(): number {
    return sortDes([...this.des().map((r) => r.de), ...this.markedDes()])
      .length;
  }

  /** The mark a message with this MTI gets for this DE, and whether it comes
   *  from the default rule (no own rule for this MTI). */
  cell(col: MappingColumn, de: string): { mark: Mark; inherited: boolean } {
    if (col.rule) return { mark: markOf(col.rule, de), inherited: false };
    if (col.key === DEFAULT_RULE) return { mark: "", inherited: false };
    return { mark: markOf(this.rule(DEFAULT_RULE), de), inherited: true };
  }

  /** Cycle a cell. A column without its own rule first gets one, copied from
   *  default, so the click changes only this DE for this MTI. */
  toggle(col: MappingColumn, de: string): void {
    if (this.readonly()) return;
    const r = col.rule ?? this.ownRule(col.key, false);
    setMark(r, de, nextMark(markOf(r, de)));
    this.changed.emit();
  }

  /** Give an MTI its own rule (a copy of default). */
  ownRule(key: string, emit = true): PresenceRule {
    const base = key === DEFAULT_RULE ? undefined : this.rule(DEFAULT_RULE);
    const r = copyRule(key, base);
    this.model().rules.push(r);
    if (emit) this.changed.emit();
    return r;
  }

  /** Drop an MTI's rule: it falls back to default again. */
  dropRule(key: string): void {
    const rules = this.model().rules;
    const i = rules.findIndex((r) => r.key === key);
    if (i >= 0) {
      rules.splice(i, 1);
      this.changed.emit();
    }
  }

  ruleCount(rule: PresenceRule | undefined): string {
    if (!rule) return "";
    const parts = [`${rule.mandatory.length} M`];
    if (rule.conditional.length) parts.push(`${rule.conditional.length} C`);
    return parts.join(" · ");
  }

  // -- summaries and warnings -----------------------------------------------

  gridSummary(): string {
    const rules = this.model().rules;
    if (!rules.length) return "no rules · nothing is checked";
    const own = rules.filter((r) => r.key !== DEFAULT_RULE).length;
    const def = this.rule(DEFAULT_RULE);
    return [
      `${own} MTI rule${own === 1 ? "" : "s"}`,
      def ? `default ${def.mandatory.length} M` : "no default",
    ].join(" · ");
  }

  /** Rules for MTIs a step cannot choose (not in the MTI list). */
  orphanRules(): string[] {
    const m = this.model();
    if (!m.mtis.length) return [];
    return m.rules
      .map((r) => r.key)
      .filter((k) => k !== DEFAULT_RULE && !m.mtis.includes(k));
  }

  /** Marked DEs the DE table does not define. */
  unknownDes(): string[] {
    const table = new Set(this.des().map((r) => String(r.de).trim()));
    return [...this.markedDes()].filter((d) => !table.has(d));
  }

  markTitle(col: MappingColumn, de: string, mark: Mark, inherited: boolean) {
    const what =
      mark === "M"
        ? "mandatory"
        : mark === "C"
          ? "conditional (recorded, not validated)"
          : "not required";
    const who = col.key === DEFAULT_RULE ? "MTIs without a rule" : col.key;
    const src = inherited ? " (from default)" : "";
    const next = this.readonly() ? "" : " · click to change";
    return `DE ${de} for ${who}: ${what}${src}${next}`;
  }
}
