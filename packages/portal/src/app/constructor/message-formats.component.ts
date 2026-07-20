import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { RouterLink } from "@angular/router";

import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { ScenarioApiService } from "./scenario-api.service";
import {
  Iso8583Field,
  MessageFormat,
  MessageFormatDraft,
  Protocol,
} from "./scenario.models";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

interface Iso8583Row extends Iso8583Field {
  de: string;
}

interface EditFormat {
  id: string;
  protocol: Protocol;
  name: string;
  version: string;
  group: string;
  description: string;
  builtin: boolean;
  isNew: boolean;
  // iso8583
  encoding: string;
  rows: Iso8583Row[];
  // iso20022
  messageType: string;
  namespace: string;
  root: string;
}

const LEN_TYPES = ["fixed", "llvar", "lllvar"];
const FIELD_TYPES = ["n", "an", "ans", "b", "z"];

/**
 * Message Formats manager — maintain versioned ISO8583 / ISO20022 specs per
 * integration. Clone a built-in to make an editable version, tweak the field
 * table (ISO8583) or message type (ISO20022), and reuse it across flows.
 */
@Component({
  selector: "app-message-formats",
  standalone: true,
  imports: [PageHeaderComponent, FormsModule, RouterLink],
  templateUrl: "./message-formats.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./message-formats.component.scss",
})
export class MessageFormatsComponent implements OnInit {
  readonly theme = inject(ThemeService);
  private readonly api = inject(ScenarioApiService);
  private readonly ui = inject(UiService);

  readonly lenTypes = LEN_TYPES;
  readonly fieldTypes = FIELD_TYPES;

  readonly formats = signal<MessageFormat[]>([]);
  readonly draft = signal<EditFormat | null>(null);
  readonly loading = signal(true);
  readonly saving = signal(false);
  readonly dirty = signal(false);
  readonly error = signal<string | null>(null);

  /** Per-protocol sections, each split into named sub-groups. */
  readonly groups = computed(() => {
    const protos: { protocol: Protocol; label: string }[] = [
      { protocol: "iso8583", label: "ISO 8583" },
      { protocol: "iso20022", label: "ISO 20022" },
    ];
    return protos.map((p) => {
      const items = this.formats().filter((f) => f.protocol === p.protocol);
      const byGroup = new Map<string, MessageFormat[]>();
      for (const f of items) {
        const key =
          (f.group ?? "").trim() || (f.builtin ? "Standard" : "Ungrouped");
        byGroup.set(key, [...(byGroup.get(key) ?? []), f]);
      }
      const rank = (n: string) =>
        n === "Standard" ? 0 : n === "Ungrouped" ? 2 : 1;
      const subgroups = [...byGroup.entries()]
        .map(([name, list]) => ({ name, items: list }))
        .sort(
          (a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name),
        );
      return { ...p, count: items.length, subgroups };
    });
  });

  /** Existing custom group names, for the editor's datalist. */
  readonly groupNames = computed(() => {
    const names = new Set<string>();
    for (const f of this.formats()) {
      const g = (f.group ?? "").trim();
      if (g && g !== "Standard") names.add(g);
    }
    return [...names].sort();
  });

  ngOnInit(): void {
    this.reload();
  }

  reload(selectId?: string): void {
    this.loading.set(true);
    this.api.formats().subscribe({
      next: (list) => {
        this.formats.set(list);
        this.loading.set(false);
        if (selectId) {
          const f = list.find((x) => x.id === selectId);
          if (f) this.draft.set(this.toEdit(f));
        }
      },
      error: () => {
        this.loading.set(false);
        this.error.set(
          "Could not load formats — is the scenario service on :8000?",
        );
      },
    });
  }

  // -- selection -----------------------------------------------------------

  isActive(f: MessageFormat): boolean {
    const d = this.draft();
    return !!d && !d.isNew && d.id === f.id;
  }

  async edit(f: MessageFormat): Promise<void> {
    if (!(await this.confirmDiscard())) return;
    this.draft.set(this.toEdit(f));
    this.dirty.set(false);
  }

  async newFormat(protocol: Protocol): Promise<void> {
    if (!(await this.confirmDiscard())) return;
    this.draft.set({
      id: "",
      protocol,
      name: "",
      version: "1",
      group: "",
      description: "",
      builtin: false,
      isNew: true,
      encoding: "ascii",
      rows:
        protocol === "iso8583"
          ? [{ de: "2", name: "PAN", len_type: "llvar", length: 19, type: "n" }]
          : [],
      messageType: protocol === "iso20022" ? "pacs.008.001.08" : "",
      namespace:
        protocol === "iso20022"
          ? "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"
          : "",
      root: "Document",
    });
    this.dirty.set(false);
  }

  async cancel(): Promise<void> {
    if (!(await this.confirmDiscard())) return;
    this.draft.set(null);
    this.dirty.set(false);
  }

  private async confirmDiscard(): Promise<boolean> {
    if (!this.dirty()) return true;
    return this.ui.confirm("Discard unsaved changes to this format?", {
      title: "Discard changes",
      confirmLabel: "Discard",
    });
  }

  markDirty(): void {
    this.dirty.set(true);
  }

  addRow(): void {
    const d = this.draft();
    if (d) {
      d.rows.push({
        de: "",
        name: "",
        len_type: "fixed",
        length: 0,
        type: "n",
      });
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

  // -- conversion ----------------------------------------------------------

  private toEdit(f: MessageFormat): EditFormat {
    const def = (f.definition ?? {}) as Record<string, any>;
    const fields = (def["fields"] ?? {}) as Record<string, Iso8583Field>;
    const rows: Iso8583Row[] = Object.entries(fields)
      .map(([de, v]) => ({
        de,
        name: v.name,
        len_type: v.len_type,
        length: v.length,
        type: v.type,
      }))
      .sort((a, b) => Number(a.de) - Number(b.de));
    return {
      id: f.id,
      protocol: f.protocol,
      name: f.name,
      version: f.version,
      group: f.group ?? "",
      description: f.description,
      builtin: f.builtin,
      isNew: false,
      encoding: def["encoding"] ?? "ascii",
      rows,
      messageType: def["message_type"] ?? "",
      namespace: def["namespace"] ?? "",
      root: def["root"] ?? "Document",
    };
  }

  private toDraft(d: EditFormat): MessageFormatDraft {
    let definition: Record<string, unknown>;
    if (d.protocol === "iso8583") {
      const fields: Record<string, Iso8583Field> = {};
      for (const r of d.rows) {
        if (!r.de.toString().trim()) continue;
        fields[r.de.toString().trim()] = {
          name: r.name,
          len_type: r.len_type,
          length: Number(r.length) || 0,
          type: r.type,
        };
      }
      definition = { encoding: d.encoding || "ascii", fields };
    } else {
      definition = {
        message_type: d.messageType,
        namespace: d.namespace,
        root: d.root || "Document",
      };
    }
    return {
      protocol: d.protocol,
      name: d.name.trim(),
      version: d.version.trim() || "1",
      group: d.group.trim(),
      description: d.description,
      definition,
    };
  }

  // -- persistence ---------------------------------------------------------

  save(): void {
    const d = this.draft();
    if (!d) return;
    if (!d.name.trim()) {
      this.ui.toast("error", "Give the format a name.");
      return;
    }
    if (d.builtin) {
      this.ui.toast(
        "error",
        "Built-in formats are read-only — use Clone to edit.",
      );
      return;
    }
    this.saving.set(true);
    const draft = this.toDraft(d);
    const req = d.isNew
      ? this.api.createFormat(draft)
      : this.api.updateFormat(d.id, draft);
    req.subscribe({
      next: (saved) => {
        this.saving.set(false);
        this.dirty.set(false);
        this.ui.toast("success", `Saved “${saved.name}” v${saved.version}`);
        this.reload(saved.id);
      },
      error: (err) => {
        this.saving.set(false);
        this.ui.toast("error", err?.error?.detail || "Save failed.");
      },
    });
  }

  clone(f: MessageFormat | EditFormat): void {
    this.api
      .cloneFormat(f.id, { name: `${f.name} (copy)`, version: "1" })
      .subscribe({
        next: (created) => {
          this.ui.toast("success", `Cloned to “${created.name}”`);
          this.dirty.set(false);
          this.reload(created.id);
        },
        error: () => this.ui.toast("error", "Clone failed."),
      });
  }

  async remove(d: EditFormat): Promise<void> {
    if (d.builtin) {
      this.ui.toast(
        "error",
        "Built-in formats cannot be deleted — clone instead.",
      );
      return;
    }
    const ok = await this.ui.confirm(
      `Delete format “${d.name}”? This cannot be undone.`,
      {
        title: "Delete format",
        confirmLabel: "Delete",
      },
    );
    if (!ok) return;
    this.api.deleteFormat(d.id).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted “${d.name}”`);
        this.draft.set(null);
        this.dirty.set(false);
        this.reload();
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }
}
