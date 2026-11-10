import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  input,
  output,
  signal,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import { IconComponent } from "../../shared/ui/icon.component";
import {
  FIELD_TYPES,
  LEN_TYPES,
  RowState,
  TableRow,
  baselineHint,
  newRow,
  rowState,
  tableFromRows,
  tableRowsFromValue,
  validateTableRow,
  visibleRows,
} from "./de-table.model";
import { DeTable, formatHint, searchDes } from "./de-values.model";

interface EditRow extends TableRow {
  key: number;
}

/**
 * The DE table a step packs with (`payload.fields`), as rows compared with a
 * baseline (the bound dialect's table, else the stock one): only what differs
 * is shown by default, each changed row says what it was and can be reverted,
 * and a DE can be added from the baseline or by number. Emits the full map on
 * every change; an empty map means "no override".
 */
@Component({
  selector: "app-de-table-editor",
  standalone: true,
  imports: [FormsModule, IconComponent],
  templateUrl: "./de-table-editor.component.html",
  styleUrl: "./de-values-editor.component.scss",
  changeDetection: ChangeDetectionStrategy.Default,
})
export class DeTableEditorComponent {
  /** The stored `fields`: an object, or a JSON string from older saves. */
  readonly value = input<unknown>(undefined);
  /** What the rows are compared with: the dialect's table or the stock one. */
  readonly baseline = input<DeTable>({});
  readonly baselineLabel = input<string>("");
  /** True when a dialect is bound: an empty override then means "the
   *  dialect's table at run time", not the connection's. */
  readonly dialectBound = input<boolean>(false);
  /** DEs the step's field values use, to flag the ones this table lacks. */
  readonly usedDes = input<string[]>([]);
  readonly valueChange = output<DeTable>();

  readonly lenTypes = LEN_TYPES;
  readonly fieldTypes = FIELD_TYPES;

  readonly mode = signal<"table" | "json">("table");
  readonly rows = signal<EditRow[]>([]);
  readonly query = signal("");
  readonly changedOnly = signal(true);
  readonly jsonDraft = signal("");
  readonly jsonError = signal<string | null>(null);
  readonly addQuery = signal("");
  readonly addOpen = signal(false);

  private nextKey = 1;
  private lastEmitted: string | null = null;

  constructor() {
    effect(() => {
      const raw = this.value();
      const parsed = tableRowsFromValue(raw);
      const incoming =
        parsed === null ? null : JSON.stringify(tableFromRows(parsed));
      if (incoming !== null && incoming === this.lastEmitted) return;
      this.lastEmitted = incoming;
      if (parsed === null) {
        this.rows.set([]);
        this.jsonDraft.set(typeof raw === "string" ? raw : "");
        this.jsonError.set("Not a JSON object yet: fix it here.");
        this.mode.set("json");
        return;
      }
      this.rows.set(parsed.map((r) => ({ ...r, key: this.nextKey++ })));
      this.jsonDraft.set(pretty(tableFromRows(parsed)));
      this.jsonError.set(null);
    });
  }

  readonly states = computed(() => {
    const base = this.baseline();
    return new Map(this.rows().map((r) => [r.key, rowState(r, base)]));
  });

  readonly changedCount = computed(
    () => [...this.states().values()].filter((s) => s !== "same").length,
  );

  readonly issueCount = computed(
    () => this.rows().filter((r) => validateTableRow(r)).length,
  );

  /** DEs the field values use that this (non-empty) table does not define. */
  readonly missingUsed = computed(() => {
    const rows = this.rows();
    if (!rows.length) return [];
    const have = new Set(rows.map((r) => r.de));
    return this.usedDes().filter((de) => !have.has(de));
  });

  /** Baseline DEs this override dropped. */
  readonly droppedCount = computed(() => {
    const rows = this.rows();
    if (!rows.length) return 0;
    const have = new Set(rows.map((r) => r.de));
    return Object.keys(this.baseline()).filter((de) => !have.has(de)).length;
  });

  readonly shown = computed(
    () =>
      visibleRows(
        this.rows(),
        this.baseline(),
        this.query(),
        this.changedOnly(),
      ) as EditRow[],
  );

  readonly summary = computed(() => {
    const n = this.rows().length;
    if (!n) return "No override";
    const parts = [`${n} DEs`];
    const c = this.changedCount();
    parts.push(c ? `${c} differ` : "same as baseline");
    const i = this.issueCount();
    if (i) parts.push(`${i} issue${i === 1 ? "" : "s"}`);
    return parts.join(" · ");
  });

  readonly addMatches = computed(() =>
    searchDes(
      this.baseline(),
      this.addQuery(),
      this.rows().map((r) => r.de),
    ),
  );

  state(r: EditRow): RowState {
    return this.states().get(r.key) ?? "same";
  }

  was(r: EditRow): string {
    return baselineHint(r.de, this.baseline());
  }

  issue(r: EditRow): string | null {
    return validateTableRow(r);
  }

  hintFor(de: string): string {
    return formatHint(this.baseline()[de]);
  }

  isKnownType(t: unknown): boolean {
    return this.fieldTypes.some((f) => f.value === t);
  }

  isKnownLenType(t: unknown): boolean {
    return (this.lenTypes as readonly string[]).includes(String(t ?? "fixed"));
  }

  // -- edits -----------------------------------------------------------------

  setKey(r: EditRow, key: string, v: unknown): void {
    if (key === "length") {
      const n = Number(v);
      r.spec.length = v === "" || v === null || Number.isNaN(n) ? undefined : n;
    } else {
      (r.spec as Record<string, unknown>)[key] = v;
    }
    this.touch();
  }

  revert(r: EditRow): void {
    const base = this.baseline()[r.de];
    if (!base) return;
    r.spec = { ...base } as EditRow["spec"];
    this.touch();
  }

  remove(r: EditRow): void {
    this.rows.set(this.rows().filter((x) => x.key !== r.key));
    this.emit();
  }

  add(de: string): void {
    const d = de.trim().replace(/^de\s*/i, "");
    this.addQuery.set("");
    this.addOpen.set(false);
    if (!d || this.rows().some((r) => r.de === d)) return;
    const row: EditRow = { ...newRow(d, this.baseline()), key: this.nextKey++ };
    this.rows.set(sortRows([...this.rows(), row]));
    // show the new row even when the list is filtered to changes
    this.query.set("");
    this.emit();
  }

  addFromQuery(): void {
    const typed = this.addQuery()
      .trim()
      .replace(/^de\s*/i, "");
    if (/^\d+$/.test(typed)) this.add(typed);
    else if (this.addMatches()[0]) this.add(this.addMatches()[0].de);
  }

  closeAddSoon(): void {
    setTimeout(() => this.addOpen.set(false), 150);
  }

  /** Re-add every baseline DE this override is missing. */
  restoreDropped(): void {
    const have = new Set(this.rows().map((r) => r.de));
    const add = Object.keys(this.baseline())
      .filter((de) => !have.has(de))
      .map((de) => ({ ...newRow(de, this.baseline()), key: this.nextKey++ }));
    this.rows.set(sortRows([...this.rows(), ...add]));
    this.emit();
  }

  /** Copy the baseline as the starting point of an override. */
  startFromBaseline(): void {
    const rows = tableRowsFromValue(this.baseline()) ?? [];
    this.rows.set(rows.map((r) => ({ ...r, key: this.nextKey++ })));
    this.emit();
  }

  clear(): void {
    this.rows.set([]);
    this.emit();
  }

  setMode(m: "table" | "json"): void {
    if (m === this.mode()) return;
    if (m === "json") {
      this.jsonDraft.set(pretty(tableFromRows(this.rows())));
      this.jsonError.set(null);
    } else {
      const parsed = tableRowsFromValue(this.jsonDraft());
      if (parsed === null) {
        this.jsonError.set(
          "Fix the JSON first: one object of DE → definition.",
        );
        return;
      }
      this.rows.set(parsed.map((r) => ({ ...r, key: this.nextKey++ })));
    }
    this.mode.set(m);
  }

  onJson(text: string): void {
    this.jsonDraft.set(text);
    const parsed = tableRowsFromValue(text.trim() ? text : "{}");
    if (parsed === null) {
      this.jsonError.set("Not valid JSON yet: one object of DE → definition.");
      return;
    }
    this.jsonError.set(null);
    this.rows.set(parsed.map((r) => ({ ...r, key: this.nextKey++ })));
    this.emit();
  }

  jsonRows(): number {
    return Math.min(20, Math.max(6, this.jsonDraft().split("\n").length + 1));
  }

  private touch(): void {
    this.rows.set([...this.rows()]);
    this.emit();
  }

  private emit(): void {
    const map = tableFromRows(this.rows());
    this.lastEmitted = JSON.stringify(map);
    this.valueChange.emit(map);
  }
}

function sortRows(rows: EditRow[]): EditRow[] {
  return [...rows].sort((a, b) => Number(a.de) - Number(b.de));
}

function pretty(map: DeTable): string {
  return Object.keys(map).length ? JSON.stringify(map, null, 2) : "";
}
