import { Component, EventEmitter, Input, Output } from "@angular/core";
import { DecimalPipe } from "@angular/common";
import { BadgeComponent, BadgeTone } from "./badge.component";
import { IconComponent } from "./icon.component";

export interface TableColumn<T = Record<string, unknown>> {
  key: string;
  header: string;
  align?: "left" | "right" | "center";
  type?: "text" | "mono" | "currency" | "number" | "badge";
  width?: string;
  sortable?: boolean;
  /** map a cell value to a badge tone (for type:'badge') */
  tone?: (value: unknown, row: T) => BadgeTone;
}

@Component({
  selector: "pp-data-table",
  standalone: true,
  imports: [DecimalPipe, BadgeComponent, IconComponent],
  template: `
    <div class="tbl-wrap">
      <table class="tbl">
        <thead>
          <tr>
            @for (col of columns; track col.key) {
              <th
                [class]="'a-' + (col.align || 'left')"
                [style.width]="col.width || null"
                [class.sortable]="col.sortable !== false"
                (click)="col.sortable !== false && sort(col.key)"
              >
                <span class="th-inner">
                  {{ col.header }}
                  @if (sortKey === col.key) {
                    <span class="caret" [class.desc]="sortDir === 'desc'">
                      <pp-icon name="chevron" [size]="14" [strokeWidth]="2.5" />
                    </span>
                  }
                </span>
              </th>
            }
          </tr>
        </thead>
        <tbody>
          @for (row of sortedRows; track $index) {
            <tr
              [class.clickable]="clickable"
              (click)="clickable && rowClick.emit(row)"
            >
              @for (col of columns; track col.key) {
                <td [class]="'a-' + (col.align || 'left')">
                  @switch (col.type) {
                    @case ("badge") {
                      <pp-badge [tone]="toneFor(col, row)">{{
                        row[col.key]
                      }}</pp-badge>
                    }
                    @case ("currency") {
                      <span class="mono"
                        >{{ currency }}
                        {{ asNumber(row[col.key]) | number: "1.0-2" }}</span
                      >
                    }
                    @case ("number") {
                      <span class="mono">{{
                        asNumber(row[col.key]) | number
                      }}</span>
                    }
                    @case ("mono") {
                      <span class="mono">{{ row[col.key] }}</span>
                    }
                    @default {
                      {{ row[col.key] }}
                    }
                  }
                </td>
              }
            </tr>
          } @empty {
            <tr>
              <td class="empty" [attr.colspan]="columns.length">
                No data to display.
              </td>
            </tr>
          }
        </tbody>
      </table>
    </div>
  `,
  styles: [
    `
      .tbl-wrap {
        overflow-x: auto;
      }
      .tbl {
        width: 100%;
        border-collapse: collapse;
        font-size: var(--text-sm);
      }
      thead th {
        position: sticky;
        top: 0;
        z-index: 1;
        background: var(--bg-subtle);
        color: var(--text-secondary);
        font-weight: var(--weight-semibold);
        font-size: var(--text-xs);
        text-transform: uppercase;
        letter-spacing: var(--tracking-wide);
        padding: var(--space-3) var(--space-4);
        white-space: nowrap;
        border-bottom: 1px solid var(--border);
      }
      th.sortable {
        cursor: pointer;
        user-select: none;
      }
      th.sortable:hover {
        color: var(--brand);
      }
      .th-inner {
        display: inline-flex;
        align-items: center;
        gap: var(--space-1);
      }
      .caret {
        display: inline-flex;
        transition: transform var(--duration-fast) var(--ease-out);
      }
      .caret.desc {
        transform: rotate(180deg);
      }
      tbody td {
        padding: var(--space-3) var(--space-4);
        border-bottom: 1px solid var(--border);
        color: var(--text-primary);
        white-space: nowrap;
      }
      tbody tr:nth-child(even) td {
        background: color-mix(in srgb, var(--bg-subtle) 45%, transparent);
      }
      tbody tr.clickable {
        cursor: pointer;
      }
      tbody tr.clickable:hover td {
        background: color-mix(in srgb, var(--brand) 6%, transparent);
      }
      tbody tr:last-child td {
        border-bottom: none;
      }
      .a-left {
        text-align: left;
      }
      .a-right {
        text-align: right;
      }
      .a-center {
        text-align: center;
      }
      td.empty {
        text-align: center;
        color: var(--text-muted);
        padding: var(--space-8);
      }
    `,
  ],
})
export class DataTableComponent<
  T extends Record<string, unknown> = Record<string, unknown>,
> {
  @Input({ required: true }) columns: TableColumn<T>[] = [];
  @Input({ required: true }) rows: T[] = [];
  @Input() currency = "₾";
  @Input() clickable = false;
  @Output() rowClick = new EventEmitter<T>();

  sortKey: string | null = null;
  sortDir: "asc" | "desc" = "asc";

  sort(key: string) {
    if (this.sortKey === key) {
      this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
    } else {
      this.sortKey = key;
      this.sortDir = "asc";
    }
  }

  get sortedRows(): T[] {
    if (!this.sortKey) return this.rows;
    const key = this.sortKey;
    const dir = this.sortDir === "asc" ? 1 : -1;
    return [...this.rows].sort((a, b) => {
      const av = a[key],
        bv = b[key];
      if (typeof av === "number" && typeof bv === "number")
        return (av - bv) * dir;
      return String(av).localeCompare(String(bv)) * dir;
    });
  }

  toneFor(col: TableColumn<T>, row: T): BadgeTone {
    return col.tone ? col.tone(row[col.key], row) : "neutral";
  }
  asNumber(v: unknown): number {
    return Number(v) || 0;
  }
}
