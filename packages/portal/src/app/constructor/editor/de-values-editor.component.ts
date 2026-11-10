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

import { RefAutocompleteDirective } from "../../shared/ref-autocomplete.directive";
import { IconComponent } from "../../shared/ui/icon.component";
import {
  AUTO_FILLED_DES,
  DE_SUGGESTIONS,
  DeRow,
  DeSpec,
  DeTable,
  formatHint,
  isReference,
  isStaticStan,
  isStaticTime,
  rowsFromValue,
  searchDes,
  sortRows,
  unknownDe,
  validateRow,
  valueFromRows,
} from "./de-values.model";

/** A row with a stable identity, so an edit never re-creates the input under
 *  the cursor. */
interface EditRow extends DeRow {
  key: number;
}

/**
 * The ISO 8583 field values of a step (`payload.values`) as a table: one row
 * per data element with its name, format and length checked against the DE
 * table the step will be packed with, one-click generators, and a JSON view
 * for pasting a whole map. Emits the map as an object on every change.
 */
@Component({
  selector: "app-de-values-editor",
  standalone: true,
  imports: [FormsModule, IconComponent, RefAutocompleteDirective],
  templateUrl: "./de-values-editor.component.html",
  styleUrl: "./de-values-editor.component.scss",
  changeDetection: ChangeDetectionStrategy.Default,
})
export class DeValuesEditorComponent {
  /** The stored `values`: an object, or a JSON string from older saves. */
  readonly value = input<unknown>(undefined);
  /** The DE table the step packs with (step snapshot, dialect or stock). */
  readonly table = input<DeTable>({});
  /** Where the table came from, for the header ("pos-dialect", "stock 1987"). */
  readonly tableLabel = input<string>("");
  /** `${...}` references for the value autocomplete. */
  readonly refs = input<string[]>([]);
  readonly valueChange = output<Record<string, string>>();

  readonly mode = signal<"table" | "json">("table");
  readonly rows = signal<EditRow[]>([]);
  readonly jsonDraft = signal("");
  readonly jsonError = signal<string | null>(null);
  readonly addQuery = signal("");
  readonly addOpen = signal(false);

  private nextKey = 1;
  /** Serialization of the last map this editor emitted: an input equal to it
   *  is our own echo and must not rebuild the rows. */
  private lastEmitted: string | null = null;

  constructor() {
    effect(() => {
      const raw = this.value();
      const parsed = rowsFromValue(raw);
      const incoming =
        parsed === null ? null : JSON.stringify(valueFromRows(parsed));
      if (incoming !== null && incoming === this.lastEmitted) return;
      this.lastEmitted = incoming;
      if (parsed === null) {
        // not a JSON object: show it raw so nothing is lost
        this.rows.set([]);
        this.jsonDraft.set(typeof raw === "string" ? raw : "");
        this.jsonError.set("Not a JSON object yet: fix it here.");
        this.mode.set("json");
        return;
      }
      this.rows.set(parsed.map((r) => ({ ...r, key: this.nextKey++ })));
      this.jsonDraft.set(pretty(valueFromRows(parsed)));
      this.jsonError.set(null);
    });
  }

  readonly present = computed(() => this.rows().map((r) => r.de));

  readonly addMatches = computed(() =>
    searchDes(this.table(), this.addQuery(), this.present()),
  );

  /** "13 fields · 2 generated · 1 issue". */
  readonly summary = computed(() => {
    const rows = this.rows();
    const refs = rows.filter((r) => isReference(r.value)).length;
    const issues = rows.filter((r) => this.issue(r) || this.unknown(r)).length;
    const parts = [`${rows.length} field${rows.length === 1 ? "" : "s"}`];
    if (refs) parts.push(`${refs} generated`);
    if (issues) parts.push(`${issues} issue${issues === 1 ? "" : "s"}`);
    return parts.join(" · ");
  });

  readonly hasIssues = computed(() =>
    this.rows().some((r) => this.issue(r) || this.unknown(r)),
  );

  /** Auto-filled DEs the step leaves out, for the footer note. */
  readonly autoMissing = computed(() => {
    const have = new Set(this.present());
    return AUTO_FILLED_DES.filter((de) => !have.has(de));
  });

  readonly autoMissingText = computed(() =>
    this.autoMissing()
      .map((de) => `DE ${de}`)
      .join(", "),
  );

  spec(r: DeRow): DeSpec | undefined {
    return this.table()[r.de];
  }

  name(r: DeRow): string {
    return this.spec(r)?.name ?? "";
  }

  hint(r: DeRow): string {
    return formatHint(this.spec(r));
  }

  issue(r: DeRow): string | null {
    return validateRow(r, this.spec(r));
  }

  unknown(r: DeRow): boolean {
    return unknownDe(r.de, this.table());
  }

  isRef(r: DeRow): boolean {
    return isReference(r.value);
  }

  /** "6/6", "16/19": the length against the spec, literals only. */
  counter(r: DeRow): string {
    const spec = this.spec(r);
    if (!spec?.length || isReference(r.value)) return "";
    return `${r.value.length}/${spec.length}`;
  }

  note(r: DeRow): string | null {
    if (isStaticStan(r))
      return "Fixed STAN: every transaction of a load run repeats it.";
    if (isStaticTime(r))
      return "Fixed time: sent unchanged on every run. Leave it out and the connection stamps it.";
    return null;
  }

  suggestions(r: DeRow): { value: string; label: string }[] {
    return (DE_SUGGESTIONS[r.de] ?? []).filter((s) => s.value !== r.value);
  }

  // -- edits -----------------------------------------------------------------

  onValue(r: EditRow, v: string): void {
    r.value = v;
    this.rows.set([...this.rows()]);
    this.emit();
  }

  useSuggestion(r: EditRow, v: string): void {
    this.onValue(r, v);
  }

  remove(r: EditRow): void {
    this.rows.set(this.rows().filter((x) => x.key !== r.key));
    this.emit();
  }

  add(de: string): void {
    const d = de.trim().replace(/^de\s*/i, "");
    if (!d) return;
    if (this.present().includes(d)) {
      this.addQuery.set("");
      this.addOpen.set(false);
      return;
    }
    const row: EditRow = {
      de: d,
      value: DE_SUGGESTIONS[d]?.[0]?.value ?? "",
      key: this.nextKey++,
    };
    // a counter / time generator is the useful default only for those DEs;
    // everything else starts empty so the author types the literal
    if (!["7", "11", "12", "13", "37"].includes(d)) row.value = "";
    this.rows.set(sortRows([...this.rows(), row]) as EditRow[]);
    this.addQuery.set("");
    this.addOpen.set(false);
    this.emit();
  }

  /** Enter in the add box: the first match, or the typed number itself. */
  addFromQuery(): void {
    const first = this.addMatches()[0];
    const typed = this.addQuery()
      .trim()
      .replace(/^de\s*/i, "");
    if (/^\d+$/.test(typed)) this.add(typed);
    else if (first) this.add(first.de);
  }

  closeAddSoon(): void {
    setTimeout(() => this.addOpen.set(false), 150);
  }

  setMode(m: "table" | "json"): void {
    if (m === this.mode()) return;
    if (m === "json") {
      this.jsonDraft.set(pretty(valueFromRows(this.rows())));
      this.jsonError.set(null);
    } else {
      const parsed = rowsFromValue(this.jsonDraft());
      if (parsed === null) {
        this.jsonError.set(
          "Fix the JSON first: it must be one object of DE → value.",
        );
        return;
      }
      this.rows.set(parsed.map((r) => ({ ...r, key: this.nextKey++ })));
    }
    this.mode.set(m);
  }

  onJson(text: string): void {
    this.jsonDraft.set(text);
    const parsed = rowsFromValue(text.trim() ? text : "{}");
    if (parsed === null) {
      this.jsonError.set("Not valid JSON yet: one object of DE → value.");
      return;
    }
    this.jsonError.set(null);
    this.rows.set(parsed.map((r) => ({ ...r, key: this.nextKey++ })));
    this.emit();
  }

  jsonRows(): number {
    return Math.min(18, Math.max(6, this.jsonDraft().split("\n").length + 1));
  }

  private emit(): void {
    const map = valueFromRows(this.rows());
    this.lastEmitted = JSON.stringify(map);
    this.valueChange.emit(map);
  }
}

function pretty(map: Record<string, string>): string {
  return Object.keys(map).length ? JSON.stringify(map, null, 2) : "";
}
