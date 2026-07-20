import { Component, Input, ChangeDetectionStrategy } from "@angular/core";

import {
  DataTableComponent,
  TableColumn,
} from "../shared/ui/data-table.component";

/** Max rows / columns rendered inline before truncating (the panel is narrow). */
const MAX_ROWS = 12;
const MAX_COLS = 6;
const MAX_CELL = 80;

type GridView =
  | {
      kind: "table";
      columns: TableColumn[];
      rows: Record<string, unknown>[];
      extra: number;
    }
  | { kind: "kv"; pairs: [string, string][] }
  | null;

/** Render a tool result as a compact grid when it's tabular.
 *
 * - array of flat objects → a table (columns inferred from the union of keys,
 *   capped; rows capped with a "+N more" note);
 * - a single object → a two-column key/value grid;
 * - anything else (scalars, arrays of scalars, empty) → nothing (the assistant's
 *   prose reply already covers those).
 */
@Component({
  selector: "pp-result-grid",
  standalone: true,
  imports: [DataTableComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (view; as v) {
      @switch (v.kind) {
        @case ("table") {
          <div class="rg">
            <pp-data-table [columns]="v.columns" [rows]="v.rows" />
            @if (v.extra > 0) {
              <div class="rg-more">+{{ v.extra }} more rows</div>
            }
          </div>
        }
        @case ("kv") {
          <table class="rg-kv">
            <tbody>
              @for (p of v.pairs; track p[0]) {
                <tr>
                  <th>{{ p[0] }}</th>
                  <td>{{ p[1] }}</td>
                </tr>
              }
            </tbody>
          </table>
        }
      }
    }
  `,
  styles: [
    `
      .rg {
        width: 100%;
      }
      .rg-more {
        font-size: var(--text-xs);
        color: var(--text-muted);
        padding: var(--space-1) 0;
      }
      .rg-kv {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--text-sm);
      }
      .rg-kv th,
      .rg-kv td {
        text-align: left;
        vertical-align: top;
        padding: 2px var(--space-2) 2px 0;
        border-bottom: 1px solid var(--border-subtle);
      }
      .rg-kv th {
        color: var(--text-secondary);
        font-weight: var(--weight-semibold);
        white-space: nowrap;
        width: 40%;
      }
      .rg-kv td {
        color: var(--text-primary);
        word-break: break-word;
        font-family: var(--font-mono);
      }
    `,
  ],
})
export class ResultGridComponent {
  view: GridView = null;

  @Input() set result(value: unknown) {
    this.view = this.build(value);
  }

  private build(value: unknown): GridView {
    if (Array.isArray(value)) {
      const objs = value.filter(
        (r) => r && typeof r === "object" && !Array.isArray(r),
      ) as Record<string, unknown>[];
      if (objs.length === 0) return null; // scalars / empty → not a grid
      const keys = [...new Set(objs.flatMap((o) => Object.keys(o)))].slice(
        0,
        MAX_COLS,
      );
      const columns: TableColumn[] = keys.map((k) => ({
        key: k,
        header: k,
        sortable: false,
      }));
      const rows = objs.slice(0, MAX_ROWS).map((o) => {
        const row: Record<string, unknown> = {};
        for (const k of keys) row[k] = this.cell(o[k]);
        return row;
      });
      return { kind: "table", columns, rows, extra: objs.length - rows.length };
    }
    if (value && typeof value === "object") {
      const pairs = Object.entries(value as Record<string, unknown>)
        .slice(0, 20)
        .map(([k, v]) => [k, this.cell(v)] as [string, string]);
      return pairs.length ? { kind: "kv", pairs } : null;
    }
    return null; // scalar — the prose reply covers it
  }

  /** Flatten any cell to a short string so it never blows out the layout. */
  private cell(v: unknown): string {
    if (v === null || v === undefined) return "";
    let s: string;
    if (typeof v === "object") {
      s = Array.isArray(v) ? `[${v.length}]` : JSON.stringify(v);
    } else {
      s = String(v);
    }
    return s.length > MAX_CELL ? s.slice(0, MAX_CELL - 1) + "…" : s;
  }
}
