import { CommonModule } from "@angular/common";
import { Component, OnInit, computed, inject, signal } from "@angular/core";
import { FormsModule } from "@angular/forms";
import { RouterLink } from "@angular/router";

import { CodeEditorComponent } from "../shared/code-editor.component";
import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { BadgeComponent } from "../shared/ui/badge.component";
import { ButtonComponent } from "../shared/ui/button.component";
import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import {
  EnvironmentConfig,
  EnvironmentSummary,
  EnvironmentsService,
} from "./environments.service";
import { ConnectionsService } from "../connections/connections.service";
import { toAdapterConfig } from "../connections/connection.models";
import {
  ADAPTER_KINDS,
  PARAM_TYPES,
  PARAM_TYPE_LABELS,
  ParamRow,
  coerceParamValue,
  inferParamType,
} from "../connections/parameter-types";

/** One adapter entry in the form view. Non-scalar settings are preserved in
 * `advanced` (and only editable in the JSON tab) so nothing is ever lost. Each
 * scalar setting is a typed {@link ParamRow} (name + type + value), the same
 * shape used by the override editors. */
interface AdapterRow {
  name: string;
  budget: string; // connection_budget.adapters[name]; "" = unset
  fields: ParamRow[];
  advanced: Record<string, unknown>;
}

interface FormModel {
  mode: string; // "", "mock", "real"
  budgetTotal: string; // connection_budget.total; "" = unset
  adapters: AdapterRow[];
  /** Top-level keys we don't surface as fields (adapter_defaults, _comment, …). */
  extraTop: Record<string, unknown>;
}

type EditorTab = "form" | "json";

interface EnvDraft {
  name: string; // registry key (slug)
  label: string;
  description: string;
  form: FormModel;
  json: string; // raw JSON mirror of the config block
  isNew: boolean;
  original?: string;
}

const NEW_TEMPLATE: EnvironmentConfig = {
  name: "",
  connection_budget: { total: 1000, adapters: { http: 600 } },
  adapters: {
    http: {
      base_url: "${vars.http_base_url}",
      api_key: "${vars.http_api_key}",
      timeout_ms: "${vars.http_timeout_ms}",
    },
  },
};

/**
 * Environments manager — create/clone/edit/delete the adapter-target profiles a
 * run executes against (dev/staging/prod). The list + full config come from the
 * orchestrator (bundled files merged with the editable registry); writes go to
 * the scenario-service registry. Editing a bundled environment creates an
 * editable copy. `${vars.*}` in config resolves from global Variables at run time.
 *
 * The config editor is a hybrid: a structured **Form** view for the common
 * fields (mode, connection budget, per-adapter scalar settings) plus a **JSON**
 * tab (Monaco) for advanced / nested config. The two views stay in sync — the
 * active tab is the source of truth and is converted on switch and on save.
 */
@Component({
  selector: "app-environments",
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    RouterLink,
    PageHeaderComponent,
    ButtonComponent,
    BadgeComponent,
    IconComponent,
    CodeEditorComponent,
  ],
  templateUrl: "./environments.component.html",
  styleUrl: "./environments.component.scss",
})
export class EnvironmentsComponent implements OnInit {
  readonly theme = inject(ThemeService);
  readonly envs = inject(EnvironmentsService);
  readonly connections = inject(ConnectionsService);
  private readonly ui = inject(UiService);

  readonly draft = signal<EnvDraft | null>(null);
  readonly tab = signal<EditorTab>("form");
  readonly dirty = signal(false);
  readonly jsonError = signal<string | null>(null);
  readonly query = signal("");

  /** Filtered + sorted environment list. */
  readonly filtered = computed(() => {
    const q = this.query().trim().toLowerCase();
    const list = this.envs.environments();
    const rows = q
      ? list.filter(
          (e) =>
            e.name.toLowerCase().includes(q) ||
            (e.label ?? "").toLowerCase().includes(q) ||
            (e.description ?? "").toLowerCase().includes(q),
        )
      : list;
    return [...rows].sort((a, b) => a.name.localeCompare(b.name));
  });

  /** `${vars.NAME}` suggestions for the value <datalist>. */
  readonly varSuggestions = computed(() =>
    this.envs.varKeys().map((k) => `\${vars.${k}}`),
  );

  ngOnInit(): void {
    this.envs.reload();
    this.envs.loadVarKeys();
    this.connections.reload();
  }

  // ---- per-connection overrides for this environment ----------------------
  // The same per-(connection, environment) overrides the Connections page edits
  // (one source of truth — the connection). Here you edit them grouped by this
  // environment; each connection is saved on its own.

  /** The environment identity overrides are keyed by (registry slug / name). */
  private envKey(): string {
    return this.draft()?.name?.trim() ?? "";
  }

  /** All saved connections, for the override editor. */
  readonly allConnections = this.connections.connections;

  /** Per-connection editing rows for this environment. Stable row objects so
   * ngModel edits don't lose focus. Reset whenever a draft opens. */
  connOverrideRows: Record<string, ParamRow[]> = {};

  readonly paramTypes = PARAM_TYPES;
  readonly paramTypeLabels = PARAM_TYPE_LABELS;
  readonly adapterKinds = ADAPTER_KINDS;

  /** Registered connection names, for the ``connection``-typed value picker. */
  connectionNames(): string[] {
    return this.allConnections().map((c) => c.name);
  }

  onParamTypeChange(row: ParamRow): void {
    if (row.type === "adapter" || row.type === "connection") row.value = "";
  }

  /** Rows for a connection, lazily initialised from its stored overrides for the
   * environment being edited; each row's type is inferred from its value. */
  rowsForConn(connName: string): ParamRow[] {
    if (!this.connOverrideRows[connName]) {
      const conn = this.allConnections().find((c) => c.name === connName);
      const ov = (conn?.environmentOverrides ?? {})[this.envKey()] ?? {};
      const names = this.connectionNames();
      this.connOverrideRows[connName] = Object.entries(ov).map(
        ([key, value]) => ({
          key,
          value: String(value),
          type: inferParamType(String(value), names),
        }),
      );
    }
    return this.connOverrideRows[connName];
  }

  addConnOverrideRow(connName: string): void {
    this.rowsForConn(connName).push({ key: "", value: "", type: "string" });
  }

  removeConnOverrideRow(connName: string, i: number): void {
    this.rowsForConn(connName).splice(i, 1);
  }

  /** Serialise + persist one connection's overrides for this environment. */
  saveConnOverride(connName: string): void {
    const key = this.envKey();
    if (!key) {
      this.ui.toast("error", "Name the environment before adding overrides.");
      return;
    }
    const obj: Record<string, unknown> = {};
    for (const r of this.connOverrideRows[connName] ?? []) {
      const k = r.key.trim();
      if (k) obj[k] = coerceParamValue(r.value, r.type);
    }
    const op = this.connections.setEnvironmentOverride(connName, key, obj);
    if (!op) return;
    op.subscribe({
      next: () =>
        this.ui.toast(
          "success",
          `Saved overrides for “${connName}” in “${key}”`,
        ),
      error: () =>
        this.ui.toast("error", "Could not save connection overrides."),
    });
  }

  /** Inheritance view (read-only): what a connection resolves to under THIS
   * environment — base ⊕ override. Mirrors the Connections-page preview and the
   * orchestrator's ``_attach_connections`` merge, so editing a host here shows
   * the effective config this environment will run against. */
  effectiveRowsForConn(connName: string): {
    key: string;
    base: string;
    effective: string;
    overridden: boolean;
  }[] {
    const conn = this.allConnections().find((c) => c.name === connName);
    if (!conn) return [];
    const base = toAdapterConfig(conn);
    const override: Record<string, unknown> = {};
    for (const r of this.rowsForConn(connName)) {
      const k = r.key.trim();
      if (k) override[k] = coerceParamValue(r.value, r.type);
    }
    const keys = Array.from(
      new Set([...Object.keys(base), ...Object.keys(override)]),
    ).sort();
    return keys.map((key) => {
      const overridden = key in override;
      return {
        key,
        base: this.fmtVal(base[key]),
        effective: this.fmtVal(overridden ? override[key] : base[key]),
        overridden,
      };
    });
  }

  private fmtVal(v: unknown): string {
    if (v === undefined) return "—";
    if (v !== null && typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  // ---- list actions -------------------------------------------------------

  newEnv(): void {
    this.openDraft({
      name: "",
      label: "",
      description: "",
      config: NEW_TEMPLATE,
      isNew: true,
    });
  }

  edit(e: EnvironmentSummary): void {
    this.envs.getConfig(e.name).subscribe({
      next: (c) =>
        this.openDraft({
          name: e.name,
          label: c.name ?? e.label ?? e.name,
          description: c.description ?? e.description ?? "",
          config: c,
          // a bundled env "edits" into a new registry copy under the same name
          isNew: e.source === "bundled",
          original: e.source === "registry" ? e.name : undefined,
        }),
      error: () => this.ui.toast("error", "Could not load environment config."),
    });
  }

  clone(e: EnvironmentSummary): void {
    this.envs.getConfig(e.name).subscribe({
      next: (c) =>
        this.openDraft({
          name: `${e.name}-copy`,
          label: `${c.name ?? e.label ?? e.name} (copy)`,
          description: c.description ?? "",
          config: c,
          isNew: true,
        }),
      error: () => this.ui.toast("error", "Could not load environment config."),
    });
  }

  isActive(e: EnvironmentSummary): boolean {
    const d = this.draft();
    return !!d && !d.isNew && d.original === e.name;
  }

  sourceTone(source: EnvironmentSummary["source"]): "neutral" | "brand" {
    return source === "registry" ? "brand" : "neutral";
  }

  /** A short, friendly summary of an environment for the list row. */
  summaryOf(e: EnvironmentSummary): string {
    return e.description?.trim() || e.label || "—";
  }

  // ---- draft lifecycle ----------------------------------------------------

  private openDraft(input: {
    name: string;
    label: string;
    description: string;
    config: EnvironmentConfig;
    isNew: boolean;
    original?: string;
  }): void {
    const block = this.configBlock(input.config);
    this.connOverrideRows = {};
    this.draft.set({
      name: input.name,
      label: input.label,
      description: input.description,
      form: this.configToForm(block),
      json: this.pretty(block),
      isNew: input.isNew,
      original: input.original,
    });
    this.tab.set("form");
    this.dirty.set(input.isNew);
    this.jsonError.set(null);
  }

  close(): void {
    this.draft.set(null);
    this.dirty.set(false);
    this.jsonError.set(null);
  }

  markDirty(): void {
    this.dirty.set(true);
  }

  /** Switch tabs, converting the current tab's content into the other view. */
  setTab(next: EditorTab): void {
    const d = this.draft();
    if (!d || next === this.tab()) return;
    if (next === "json") {
      // form -> json: serialise the structured model
      d.json = this.pretty(this.formToConfig(d.form));
      this.jsonError.set(null);
    } else {
      // json -> form: parse text, rebuild the structured model
      const parsed = this.tryParse(d.json);
      if (parsed === undefined) {
        this.ui.toast("error", "Fix the JSON before switching to the form.");
        return;
      }
      d.form = this.configToForm(parsed);
    }
    this.draft.set({ ...d });
    this.tab.set(next);
  }

  onJsonChange(text: string): void {
    const d = this.draft();
    if (!d) return;
    d.json = text;
    this.jsonError.set(
      this.tryParse(text) === undefined ? "Invalid JSON" : null,
    );
    this.markDirty();
  }

  // ---- adapter / field editing (form view) --------------------------------

  addAdapter(): void {
    const d = this.draft();
    if (!d) return;
    d.form.adapters.push({
      name: "new_adapter",
      budget: "",
      fields: [],
      advanced: {},
    });
    this.draft.set({ ...d });
    this.markDirty();
  }

  removeAdapter(i: number): void {
    const d = this.draft();
    if (!d) return;
    d.form.adapters.splice(i, 1);
    this.draft.set({ ...d });
    this.markDirty();
  }

  addField(a: AdapterRow): void {
    a.fields.push({ key: "", value: "", type: "string" });
    this.draft.set({ ...this.draft()! });
    this.markDirty();
  }

  removeField(a: AdapterRow, i: number): void {
    a.fields.splice(i, 1);
    this.draft.set({ ...this.draft()! });
    this.markDirty();
  }

  advancedCount(a: AdapterRow): number {
    return Object.keys(a.advanced).length;
  }

  extraTopKeys(): string[] {
    return Object.keys(this.draft()?.form.extraTop ?? {});
  }

  // ---- save / delete ------------------------------------------------------

  save(): void {
    const d = this.draft();
    if (!d) return;
    if (!d.name.trim())
      return this.ui.toast("error", "Give the environment a name.");

    let config: Record<string, unknown>;
    if (this.tab() === "json") {
      const parsed = this.tryParse(d.json);
      if (parsed === undefined)
        return this.ui.toast("error", "Config is not valid JSON.");
      config = parsed;
    } else {
      config = this.formToConfig(d.form);
    }

    const doc = {
      ...config,
      name: d.label || d.name,
      description: d.description,
    };
    this.envs.save(d.name, doc, d.original).subscribe({
      next: () => {
        this.ui.toast("success", `Saved environment “${d.name}”`);
        this.close();
      },
      error: (e) => this.ui.toast("error", e?.error?.detail ?? "Save failed."),
    });
  }

  async remove(e: EnvironmentSummary): Promise<void> {
    if (e.source === "bundled")
      return this.ui.toast("error", "Bundled environments can't be deleted.");
    if (
      !(await this.ui.confirm(`Delete environment “${e.name}”?`, {
        danger: true,
      }))
    )
      return;
    this.envs.remove(e.name).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted “${e.name}”`);
        if (this.draft()?.original === e.name) this.close();
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }

  // ---- conversions --------------------------------------------------------

  /** Strip label/description (and name) from a config doc, leaving adapter config. */
  private configBlock(c: EnvironmentConfig): Record<string, unknown> {
    const { name, description, ...rest } = c;
    return rest;
  }

  private pretty(o: unknown): string {
    return JSON.stringify(o, null, 2);
  }

  private tryParse(text: string): Record<string, unknown> | undefined {
    try {
      const v = JSON.parse(text || "{}");
      return v && typeof v === "object" && !Array.isArray(v) ? v : {};
    } catch {
      return undefined;
    }
  }

  /** Build the structured form model from a raw config block. */
  private configToForm(cfg: Record<string, unknown>): FormModel {
    const budget = (cfg["connection_budget"] as Record<string, unknown>) ?? {};
    const budgetAdapters =
      (budget["adapters"] as Record<string, unknown>) ?? {};
    const adaptersObj = (cfg["adapters"] as Record<string, unknown>) ?? {};

    const adapters: AdapterRow[] = Object.entries(adaptersObj).map(
      ([name, raw]) => {
        const fields: ParamRow[] = [];
        const advanced: Record<string, unknown> = {};
        const names = this.connectionNames();
        if (raw && typeof raw === "object" && !Array.isArray(raw)) {
          for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
            if (typeof v === "string")
              // string values may be an adapter kind / connection reference
              fields.push({ key: k, value: v, type: inferParamType(v, names) });
            else if (typeof v === "number")
              fields.push({ key: k, value: String(v), type: "number" });
            else if (typeof v === "boolean")
              fields.push({ key: k, value: String(v), type: "boolean" });
            else advanced[k] = v;
          }
        } else if (raw !== undefined) {
          advanced["__value"] = raw;
        }
        const b = budgetAdapters[name];
        return { name, budget: b != null ? String(b) : "", fields, advanced };
      },
    );

    const extraTop: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(cfg)) {
      if (k === "mode" || k === "connection_budget" || k === "adapters")
        continue;
      extraTop[k] = v;
    }

    return {
      mode: typeof cfg["mode"] === "string" ? (cfg["mode"] as string) : "",
      budgetTotal: budget["total"] != null ? String(budget["total"]) : "",
      adapters,
      extraTop,
    };
  }

  /** Rebuild a raw config block from the structured form model. */
  private formToConfig(form: FormModel): Record<string, unknown> {
    const config: Record<string, unknown> = { ...form.extraTop };
    if (form.mode) config["mode"] = form.mode;

    const budgetAdapters: Record<string, unknown> = {};
    for (const a of form.adapters) {
      if (a.name.trim() && a.budget.trim() !== "")
        budgetAdapters[a.name] = this.numOrSelf(a.budget);
    }
    const cb: Record<string, unknown> = {};
    if (form.budgetTotal.trim() !== "")
      cb["total"] = this.numOrSelf(form.budgetTotal);
    if (Object.keys(budgetAdapters).length) cb["adapters"] = budgetAdapters;
    if (Object.keys(cb).length) config["connection_budget"] = cb;

    const adapters: Record<string, unknown> = {};
    for (const a of form.adapters) {
      if (!a.name.trim()) continue;
      const obj: Record<string, unknown> = { ...a.advanced };
      for (const f of a.fields) {
        if (!f.key.trim()) continue;
        obj[f.key] = coerceParamValue(f.value, f.type);
      }
      adapters[a.name] = obj;
    }
    config["adapters"] = adapters;
    return config;
  }

  /** Numbers become numbers; anything else (e.g. `${vars.x}`) stays a string. */
  private numOrSelf(value: string): unknown {
    const t = value.trim();
    if (t === "" || /[^0-9.+-]/.test(t)) return value;
    const n = Number(t);
    return Number.isFinite(n) ? n : value;
  }
}
