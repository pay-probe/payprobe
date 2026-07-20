import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";
import { firstValueFrom } from "rxjs";

import { ScenarioApiService } from "../constructor/scenario-api.service";
import {
  ActionSpec,
  FlowStep,
  StarterFlow,
  StarterFlowDraft,
  TargetSpec,
} from "../constructor/scenario.models";
import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

interface KV {
  key: string;
  value: string;
}

interface EditStep {
  ref: string;
  target: string;
  action: string;
  config: KV[];
  inputs: KV[];
}

interface EditFlow {
  id?: string;
  label: string;
  description: string;
  builtin: boolean;
  steps: EditStep[];
}

interface StepIssue {
  index: number;
  level: "error" | "warn";
  text: string;
}

/**
 * Starter Flows manager — the one-click, pre-wired chains the scenario editor
 * offers in its palette. Catalog-driven step pickers, key/value config & input
 * editors with `${ref.…}` suggestions, drag-to-reorder, and a live preview of
 * the chain with validation. Persisted in the scenario service.
 */
@Component({
  selector: "app-starter-flows",
  standalone: true,
  imports: [PageHeaderComponent, FormsModule, IconComponent],
  templateUrl: "./starter-flows.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./starter-flows.component.scss",
})
export class StarterFlowsComponent implements OnInit {
  readonly theme = inject(ThemeService);
  private readonly api = inject(ScenarioApiService);
  private readonly ui = inject(UiService);

  readonly flows = signal<StarterFlow[]>([]);
  readonly catalog = signal<TargetSpec[]>([]);
  readonly loading = signal(true);
  readonly draft = signal<EditFlow | null>(null);
  readonly originalId = signal<string | null>(null);
  readonly dirty = signal(false);
  readonly dragIndex = signal<number | null>(null);
  readonly importing = signal(false);

  ngOnInit(): void {
    this.api.catalog().subscribe({
      next: (c) => this.catalog.set(c),
      error: () => this.catalog.set([]),
    });
    this.reload();
  }

  reload(select?: string): void {
    this.loading.set(true);
    this.api.starterFlows().subscribe({
      next: (list) => {
        this.flows.set(list);
        this.loading.set(false);
        if (select) {
          const f = list.find((x) => x.id === select);
          if (f) this.edit(f);
        }
      },
      error: () => this.loading.set(false),
    });
  }

  // -- import --------------------------------------------------------------

  /** Import one or more flows from a JSON file. Accepts either a single
   *  StarterFlowDraft object or an array of them. Each is created via the
   *  scenario service (POST /starter-flows); existing flows are untouched. */
  async onImportFile(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = ""; // allow re-importing the same file
    if (!file) return;

    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      this.ui.toast("error", "Import failed: file is not valid JSON.");
      return;
    }

    const drafts = (
      Array.isArray(parsed) ? parsed : [parsed]
    ) as StarterFlowDraft[];
    const valid = drafts.filter(
      (d) =>
        d &&
        typeof d.label === "string" &&
        d.label.trim() &&
        Array.isArray(d.steps),
    );
    if (valid.length === 0) {
      this.ui.toast(
        "error",
        "Import failed: no flows with a label and steps found.",
      );
      return;
    }

    this.importing.set(true);
    let ok = 0;
    let lastId: string | undefined;
    for (const d of valid) {
      const draft: StarterFlowDraft = {
        label: d.label.trim(),
        description: d.description ?? "",
        steps: (d.steps ?? []).map((s) => ({
          ref: s.ref ?? "",
          target: s.target ?? "",
          action: s.action ?? "",
          config: s.config ?? {},
          inputs: s.inputs ?? {},
        })),
      };
      try {
        const saved = await firstValueFrom(this.api.createStarterFlow(draft));
        lastId = saved.id;
        ok += 1;
      } catch {
        /* keep going; report the tally at the end */
      }
    }
    this.importing.set(false);

    const failed = valid.length - ok;
    if (ok > 0) {
      this.ui.toast(
        failed ? "error" : "success",
        `Imported ${ok} flow${ok === 1 ? "" : "s"}${failed ? `, ${failed} failed` : ""}.`,
      );
      this.reload(ok === 1 ? lastId : undefined);
    } else {
      this.ui.toast(
        "error",
        "Import failed — is the scenario service running?",
      );
    }
  }

  // -- catalog helpers -----------------------------------------------------

  targetLabel(target: string): string {
    return this.catalog().find((t) => t.target === target)?.label ?? target;
  }

  actionsFor(target: string): ActionSpec[] {
    return this.catalog().find((t) => t.target === target)?.actions ?? [];
  }

  actionLabel(target: string, action: string): string {
    return (
      this.actionsFor(target).find((a) => a.name === action)?.label ?? action
    );
  }

  private responseFields(target: string, action: string): string[] {
    return (
      this.actionsFor(target).find((a) => a.name === action)?.response_fields ??
      []
    );
  }

  /** `${ref.response.field}` suggestions from all steps before `index`. */
  refSuggestions(index: number): string[] {
    const d = this.draft();
    if (!d) return [];
    const out: string[] = [];
    for (let j = 0; j < index; j++) {
      const s = d.steps[j];
      if (!s.ref) continue;
      const fields = this.responseFields(s.target, s.action);
      if (fields.length)
        out.push(...fields.map((f) => `\${${s.ref}.response.${f}}`));
      else out.push(`\${${s.ref}.response.}`);
    }
    return out;
  }

  // -- selection / lifecycle ----------------------------------------------

  isActive(f: StarterFlow): boolean {
    return this.originalId() === f.id && this.draft() !== null;
  }

  newFlow(): void {
    this.draft.set({ label: "", description: "", builtin: false, steps: [] });
    this.originalId.set(null);
    this.dirty.set(false);
  }

  edit(f: StarterFlow): void {
    this.draft.set({
      id: f.id,
      label: f.label,
      description: f.description,
      builtin: f.builtin,
      steps: (f.steps ?? []).map((s) => this.toEditStep(s)),
    });
    this.originalId.set(f.id);
    this.dirty.set(false);
  }

  cancel(): void {
    this.draft.set(null);
    this.originalId.set(null);
    this.dirty.set(false);
  }

  markDirty(): void {
    this.dirty.set(true);
  }

  // -- step editing --------------------------------------------------------

  addStep(): void {
    const d = this.draft();
    if (!d) return;
    d.steps.push({ ref: "", target: "", action: "", config: [], inputs: [] });
    this.markDirty();
  }

  removeStep(i: number): void {
    const d = this.draft();
    if (!d) return;
    d.steps.splice(i, 1);
    this.markDirty();
  }

  /** Target changed: clear an action that no longer belongs to the target. */
  onTargetChange(s: EditStep): void {
    if (
      s.action &&
      !this.actionsFor(s.target).some((a) => a.name === s.action)
    ) {
      s.action = "";
    }
    this.markDirty();
  }

  /** Action chosen: suggest a ref + seed config rows from the action params. */
  onActionChange(s: EditStep): void {
    if (!s.ref.trim() && s.action) s.ref = this.suggestRef(s.action);
    this.markDirty();
  }

  private suggestRef(action: string): string {
    const base = action.replace(/^send_/, "").split("_")[0] || action;
    const used = new Set(
      (this.draft()?.steps ?? []).map((x) => x.ref).filter(Boolean),
    );
    if (!used.has(base)) return base;
    let n = 2;
    while (used.has(`${base}${n}`)) n++;
    return `${base}${n}`;
  }

  addRow(rows: KV[]): void {
    rows.push({ key: "", value: "" });
    this.markDirty();
  }

  removeRow(rows: KV[], i: number): void {
    rows.splice(i, 1);
    this.markDirty();
  }

  // -- drag reorder --------------------------------------------------------

  onDragStart(i: number): void {
    this.dragIndex.set(i);
  }

  onDrop(i: number): void {
    const from = this.dragIndex();
    const d = this.draft();
    this.dragIndex.set(null);
    if (d === null || from === null || from === i) return;
    const [moved] = d.steps.splice(from, 1);
    d.steps.splice(i, 0, moved);
    this.markDirty();
  }

  moveStep(i: number, dir: -1 | 1): void {
    const d = this.draft();
    if (!d) return;
    const j = i + dir;
    if (j < 0 || j >= d.steps.length) return;
    [d.steps[i], d.steps[j]] = [d.steps[j], d.steps[i]];
    this.markDirty();
  }

  // -- validation / preview -----------------------------------------------

  readonly issues = computed<StepIssue[]>(() => {
    const d = this.draft();
    if (!d) return [];
    const out: StepIssue[] = [];
    const seen = new Set<string>();
    const defined = new Set<string>();
    d.steps.forEach((s, i) => {
      if (!s.ref.trim())
        out.push({ index: i, level: "error", text: "missing ref" });
      else if (seen.has(s.ref))
        out.push({
          index: i,
          level: "error",
          text: `duplicate ref “${s.ref}”`,
        });
      seen.add(s.ref);
      if (!s.target)
        out.push({ index: i, level: "error", text: "no target selected" });
      if (!s.action)
        out.push({ index: i, level: "error", text: "no action selected" });
      // unknown refs in config/inputs values must resolve to an earlier step
      for (const kv of [...s.config, ...s.inputs]) {
        for (const ref of this.extractRefs(kv.value)) {
          if (!defined.has(ref)) {
            out.push({
              index: i,
              level: "warn",
              text: `“${kv.key || "value"}” references ${ref} which isn't defined earlier`,
            });
          }
        }
      }
      if (s.ref) defined.add(s.ref);
    });
    return out;
  });

  issuesFor(i: number): StepIssue[] {
    return this.issues().filter((x) => x.index === i);
  }

  hasErrors(): boolean {
    return this.issues().some((x) => x.level === "error");
  }

  private extractRefs(value: string): string[] {
    const out: string[] = [];
    const re = /\$\{([A-Za-z0-9_-]+)\./g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(value)) !== null) out.push(m[1]);
    return out;
  }

  // -- save / delete -------------------------------------------------------

  save(): void {
    const d = this.draft();
    if (!d) return;
    if (!d.label.trim()) {
      this.ui.toast("error", "Give the flow a label.");
      return;
    }
    if (this.hasErrors()) {
      this.ui.toast("error", "Fix the highlighted step errors first.");
      return;
    }
    const draft: StarterFlowDraft = {
      label: d.label.trim(),
      description: d.description,
      steps: d.steps.map((s) => this.toFlowStep(s)),
    };
    const id = this.originalId();
    const req = id
      ? this.api.updateStarterFlow(id, draft)
      : this.api.createStarterFlow(draft);
    req.subscribe({
      next: (saved) => {
        this.dirty.set(false);
        this.ui.toast("success", `Saved flow “${saved.label}”`);
        this.reload(saved.id);
      },
      error: () =>
        this.ui.toast("error", "Save failed — is the service running?"),
    });
  }

  removeCurrent(): void {
    const id = this.originalId();
    const f = id ? this.flows().find((x) => x.id === id) : null;
    if (f) void this.remove(f);
  }

  async remove(f: StarterFlow): Promise<void> {
    const reset = f.builtin;
    const ok = await this.ui.confirm(
      reset
        ? `Reset built-in flow “${f.label}” to its default?`
        : `Delete flow “${f.label}”?`,
      {
        title: reset ? "Reset flow" : "Delete flow",
        confirmLabel: reset ? "Reset" : "Delete",
        danger: !reset,
      },
    );
    if (!ok) return;
    this.api.deleteStarterFlow(f.id).subscribe({
      next: () => {
        if (this.originalId() === f.id) this.cancel();
        this.ui.toast(
          "success",
          reset ? "Reset to default." : `Deleted “${f.label}”`,
        );
        this.reload();
      },
      error: () =>
        this.ui.toast(
          "error",
          reset ? "Nothing to reset (no overrides)." : "Delete failed.",
        ),
    });
  }

  // -- model <-> edit conversions -----------------------------------------

  private toEditStep(s: FlowStep): EditStep {
    return {
      ref: s.ref ?? "",
      target: s.target ?? "",
      action: s.action ?? "",
      config: this.toRows(s.config),
      inputs: this.toRows(s.inputs as Record<string, unknown> | undefined),
    };
  }

  private toRows(obj?: Record<string, unknown>): KV[] {
    if (!obj) return [];
    return Object.entries(obj).map(([key, value]) => ({
      key,
      value: this.stringifyValue(value),
    }));
  }

  private toFlowStep(s: EditStep): FlowStep {
    const config: Record<string, unknown> = {};
    for (const { key, value } of s.config) {
      if (key.trim()) config[key.trim()] = this.coerceValue(value);
    }
    const inputs: Record<string, string> = {};
    for (const { key, value } of s.inputs) {
      if (key.trim()) inputs[key.trim()] = value;
    }
    return {
      ref: s.ref.trim(),
      target: s.target,
      action: s.action,
      config,
      inputs,
    };
  }

  private stringifyValue(v: unknown): string {
    if (v === null || v === undefined) return "";
    if (typeof v === "string") return v;
    return JSON.stringify(v);
  }

  /** Coerce an edited string back to number/bool/JSON where it clearly is one. */
  private coerceValue(value: string): unknown {
    const t = value.trim();
    if (t === "") return "";
    if (/^-?\d+$/.test(t)) return Number(t);
    if (t === "true" || t === "false") return t === "true";
    if (t.startsWith("{") || t.startsWith("[")) {
      try {
        return JSON.parse(t);
      } catch {
        return value;
      }
    }
    return value;
  }
}
