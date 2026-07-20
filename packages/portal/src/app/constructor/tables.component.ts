import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { RouterLink } from "@angular/router";

import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { ScenarioApiService } from "./scenario-api.service";
import { DataTable } from "./scenario.models";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

interface EditTable {
  name: string;
  description: string;
  columns: string[];
  /** Rows as cell arrays aligned to `columns` (renaming a column never re-keys). */
  rows: string[][];
  isNew: boolean;
}

/**
 * Tables manager — global named data tables (card profiles, BIN ranges, lookup
 * maps…). Injected into runs as ${tables.NAME}; read a row with the
 * "Data Tables → Table row lookup" step.
 */
@Component({
  selector: "app-tables",
  standalone: true,
  imports: [PageHeaderComponent, FormsModule, RouterLink],
  templateUrl: "./tables.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./tables.component.scss",
})
export class TablesComponent implements OnInit {
  readonly theme = inject(ThemeService);
  private readonly api = inject(ScenarioApiService);
  private readonly ui = inject(UiService);

  readonly tables = signal<DataTable[]>([]);
  readonly draft = signal<EditTable | null>(null);
  readonly loading = signal(true);
  readonly saving = signal(false);
  readonly dirty = signal(false);

  ngOnInit(): void {
    this.reload();
  }

  reload(select?: string): void {
    this.loading.set(true);
    this.api.tables().subscribe({
      next: (t) => {
        this.tables.set(t);
        this.loading.set(false);
        if (select) {
          const found = t.find((x) => x.name === select);
          if (found) this.draft.set(this.toEdit(found));
        }
      },
      error: () => this.loading.set(false),
    });
  }

  private toEdit(t: DataTable): EditTable {
    const columns = [...t.columns];
    return {
      name: t.name,
      description: t.description,
      columns,
      rows: t.rows.map((r) =>
        columns.map((c) => {
          const v = r[c];
          return v === undefined || v === null ? "" : String(v);
        }),
      ),
      isNew: false,
    };
  }

  isActive(t: DataTable): boolean {
    const d = this.draft();
    return !!d && !d.isNew && d.name === t.name;
  }

  edit(t: DataTable): void {
    this.draft.set(this.toEdit(t));
    this.dirty.set(false);
  }

  newTable(): void {
    this.draft.set({
      name: "",
      description: "",
      columns: ["key", "value"],
      rows: [["", ""]],
      isNew: true,
    });
    this.dirty.set(false);
  }

  cancel(): void {
    this.draft.set(null);
    this.dirty.set(false);
  }

  markDirty(): void {
    this.dirty.set(true);
  }

  // -- columns / rows (index-aligned; renaming never touches row data) -----

  setColumn(i: number, name: string): void {
    const d = this.draft();
    if (d) {
      d.columns[i] = name;
      this.markDirty();
    }
  }

  addColumn(): void {
    const d = this.draft();
    if (!d) return;
    d.columns.push("col" + (d.columns.length + 1));
    for (const r of d.rows) r.push("");
    this.markDirty();
  }

  removeColumn(i: number): void {
    const d = this.draft();
    if (!d || d.columns.length <= 1) return;
    d.columns.splice(i, 1);
    for (const r of d.rows) r.splice(i, 1);
    this.markDirty();
  }

  addRow(): void {
    const d = this.draft();
    if (d) {
      d.rows.push(d.columns.map(() => ""));
      this.markDirty();
    }
  }

  removeRow(i: number): void {
    const d = this.draft();
    if (d) {
      d.rows.splice(i, 1);
      this.markDirty();
    }
  }

  setCell(row: string[], ci: number, value: string): void {
    row[ci] = value;
    this.markDirty();
  }

  // -- persistence ---------------------------------------------------------

  save(): void {
    const d = this.draft();
    if (!d) return;
    if (!d.name.trim()) {
      this.ui.toast("error", "Give the table a name.");
      return;
    }
    const columns = d.columns.map((c) => c.trim()).filter(Boolean);
    const rows = d.rows.map((cells) => {
      const obj: Record<string, unknown> = {};
      d.columns.forEach((c, i) => {
        if (c.trim()) obj[c.trim()] = cells[i] ?? "";
      });
      return obj;
    });
    this.saving.set(true);
    this.api
      .saveTable(d.name.trim(), { description: d.description, columns, rows })
      .subscribe({
        next: (saved) => {
          this.saving.set(false);
          this.dirty.set(false);
          this.ui.toast("success", `Saved table “${saved.name}”`);
          this.reload(saved.name);
        },
        error: () => {
          this.saving.set(false);
          this.ui.toast("error", "Save failed.");
        },
      });
  }

  async remove(d: EditTable): Promise<void> {
    const ok = await this.ui.confirm(`Delete table “${d.name}”?`, {
      title: "Delete table",
      confirmLabel: "Delete",
    });
    if (!ok) return;
    this.api.deleteTable(d.name).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted “${d.name}”`);
        this.draft.set(null);
        this.reload();
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }
}
