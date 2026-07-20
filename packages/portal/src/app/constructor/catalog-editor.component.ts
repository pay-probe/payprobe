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

import { CodeEditorComponent } from "../shared/code-editor.component";
import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { ScenarioApiService } from "./scenario-api.service";
import {
  CODE_LANGUAGES,
  CRYPTO_OPERATIONS,
  CodeLanguage,
  CryptoOperation,
  HTTP_METHODS,
  HttpMethod,
  KeyValue,
  ManagedTarget,
  NodeConfig,
  StepBehaviorKind,
  TargetSpec,
} from "./scenario.models";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { IconComponent } from "../shared/ui/icon.component";

/** A typed, selectable parameter being edited in the catalog UI. */
interface EditParam {
  name: string;
  label: string;
  type: "string" | "number" | "boolean" | "enum" | "json" | "format";
  options: string; // comma-separated, for enum
  required: boolean;
  default: string;
}

interface EditAction {
  name: string;
  label: string;
  paramRows: EditParam[];
  responseText: string;
  behaviorKind: StepBehaviorKind;
  method: string;
  url: string;
  headerRows: KeyValue[];
  body: string;
  language: CodeLanguage;
  code: string;
  // crypto behaviour (HSM operations) — operation is editable; the rest of the
  // template (preset field defaults) is preserved opaquely so it round-trips.
  cryptoOp: CryptoOperation;
  cryptoTemplate: Record<string, unknown>;
  open: boolean;
}

const PARAM_TYPES: EditParam["type"][] = [
  "string",
  "number",
  "boolean",
  "enum",
  "json",
  "format",
];

interface EditTarget {
  target: string;
  label: string;
  category: string;
  color: string;
  description: string;
  icon: string;
  builtin: boolean;
  overridden: boolean;
  hidden: boolean;
  isNew: boolean;
  actions: EditAction[];
}

type FilterMode = "all" | "builtin" | "custom" | "hidden";

/**
 * Step Types editor — CRUD for the palette catalog. Add custom targets/actions,
 * override or hide built-ins, and back a custom step with an HTTP request or a
 * code snippet so it runs without a backend adapter.
 */
@Component({
  selector: "app-catalog-editor",
  standalone: true,
  imports: [
    PageHeaderComponent,
    FormsModule,
    RouterLink,
    CodeEditorComponent,
    IconComponent,
  ],
  templateUrl: "./catalog-editor.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./catalog-editor.component.scss",
})
export class CatalogEditorComponent implements OnInit {
  readonly theme = inject(ThemeService);
  private readonly api = inject(ScenarioApiService);
  private readonly ui = inject(UiService);

  readonly httpMethods = HTTP_METHODS;
  readonly codeLanguages = CODE_LANGUAGES;
  readonly cryptoOperations = CRYPTO_OPERATIONS;
  readonly behaviorKinds: {
    value: StepBehaviorKind;
    label: string;
    icon: string;
    hint: string;
  }[] = [
    {
      value: "adapter",
      label: "Adapter",
      icon: "plug",
      hint: "Backend adapter (built-ins)",
    },
    { value: "http", label: "HTTP", icon: "globe", hint: "Call a REST API" },
    {
      value: "code",
      label: "Code",
      icon: "terminal",
      hint: "Run a code snippet",
    },
    {
      value: "crypto",
      label: "Crypto (HSM)",
      icon: "lock",
      hint: "Pre-configured HSM operation",
    },
  ];
  /** Category logos offered for a step type (names from the pp-icon set). */
  readonly iconOptions: { value: string; label: string }[] = [
    { value: "", label: "— none (color dot) —" },
    { value: "globe", label: "Globe · HTTP / REST" },
    { value: "network", label: "Network · ISO 8583 / TCP" },
    { value: "message", label: "Message · NATS / messaging" },
    { value: "lock", label: "Lock · HSM" },
    { value: "key", label: "Key · crypto / EMV crypto" },
    { value: "shield", label: "Shield · security" },
    { value: "database", label: "Database" },
    { value: "table", label: "Table · data tables" },
    { value: "layers", label: "Layers · tools / EMV" },
    { value: "format", label: "Format · ISO messaging" },
    { value: "sparkles", label: "Sparkles · intelligence" },
    { value: "cpu", label: "CPU · compute" },
    { value: "server", label: "Server" },
    { value: "plug", label: "Plug · adapter" },
    { value: "zap", label: "Zap · events" },
    { value: "terminal", label: "Terminal · code" },
  ];
  readonly filters: { value: FilterMode; label: string }[] = [
    { value: "all", label: "All" },
    { value: "builtin", label: "Built-in" },
    { value: "custom", label: "Custom" },
    { value: "hidden", label: "Hidden" },
  ];

  readonly targets = signal<ManagedTarget[]>([]);
  readonly draft = signal<EditTarget | null>(null);
  readonly loading = signal(true);
  readonly saving = signal(false);
  readonly error = signal<string | null>(null);
  readonly dirty = signal(false);

  readonly search = signal("");
  readonly filterMode = signal<FilterMode>("all");

  readonly counts = computed(() => {
    const t = this.targets();
    return {
      all: t.length,
      builtin: t.filter((x) => x.builtin).length,
      custom: t.filter((x) => !x.builtin).length,
      hidden: t.filter((x) => x.hidden).length,
    };
  });

  /** Targets filtered by search + mode, grouped by category. */
  readonly groups = computed(() => {
    const q = this.search().trim().toLowerCase();
    const mode = this.filterMode();
    const list = this.targets().filter((t) => {
      if (mode === "builtin" && !t.builtin) return false;
      if (mode === "custom" && t.builtin) return false;
      if (mode === "hidden" && !t.hidden) return false;
      if (
        q &&
        !`${t.label} ${t.target} ${t.category}`.toLowerCase().includes(q)
      )
        return false;
      return true;
    });
    const map = new Map<string, ManagedTarget[]>();
    for (const t of list) {
      const key = t.category || "Other";
      map.set(key, [...(map.get(key) ?? []), t]);
    }
    return [...map.entries()]
      .map(([category, items]) => ({ category, items }))
      .sort((a, b) => a.category.localeCompare(b.category));
  });

  ngOnInit(): void {
    this.reload();
  }

  reload(selectTarget?: string): void {
    this.loading.set(true);
    this.api.manageCatalog().subscribe({
      next: (res) => {
        this.targets.set(res.targets);
        this.loading.set(false);
        if (selectTarget) {
          const t = res.targets.find((x) => x.target === selectTarget);
          if (t) this.draft.set(this.toEdit(t));
        }
      },
      error: () => {
        this.loading.set(false);
        this.error.set(
          "Could not load the catalog — is the scenario service running on :8000?",
        );
      },
    });
  }

  // -- selection / draft ---------------------------------------------------

  isActive(t: ManagedTarget): boolean {
    const d = this.draft();
    return !!d && !d.isNew && d.target === t.target;
  }

  /** Reset the editor scroll when switching between drafts. */
  private scrollEditorTop(): void {
    setTimeout(() =>
      document
        .querySelector(".editor__scroll")
        ?.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior }),
    );
  }

  async edit(t: ManagedTarget): Promise<void> {
    if (!(await this.confirmDiscard())) return;
    this.draft.set(this.toEdit(t));
    this.dirty.set(false);
    this.scrollEditorTop();
  }

  async newTarget(): Promise<void> {
    if (!(await this.confirmDiscard())) return;
    this.draft.set({
      target: "",
      label: "",
      category: "Custom",
      color: "#7ee787",
      description: "",
      icon: "",
      builtin: false,
      overridden: false,
      hidden: false,
      isNew: true,
      actions: [this.newAction()],
    });
    this.dirty.set(false);
    this.scrollEditorTop();
  }

  async cancel(): Promise<void> {
    if (!(await this.confirmDiscard())) return;
    this.draft.set(null);
    this.dirty.set(false);
  }

  private async confirmDiscard(): Promise<boolean> {
    if (!this.dirty()) return true;
    return this.ui.confirm("Discard unsaved changes to this step type?", {
      title: "Discard changes",
      confirmLabel: "Discard",
    });
  }

  markDirty(): void {
    this.dirty.set(true);
  }

  readonly paramTypes = PARAM_TYPES;

  private newAction(): EditAction {
    return {
      name: "",
      label: "",
      paramRows: [],
      responseText: "",
      behaviorKind: "http",
      method: "GET",
      url: "",
      headerRows: [],
      body: "",
      language: "python",
      code: "return {}",
      cryptoOp: "pin_block_encode",
      cryptoTemplate: {},
      open: true,
    };
  }

  addAction(): void {
    const d = this.draft();
    if (!d) return;
    d.actions.forEach((a) => (a.open = false));
    d.actions.push(this.newAction());
    this.markDirty();
  }

  removeAction(i: number): void {
    const d = this.draft();
    if (d) {
      d.actions.splice(i, 1);
      this.markDirty();
    }
  }

  toggleAction(a: EditAction): void {
    a.open = !a.open;
  }

  setBehavior(a: EditAction, kind: StepBehaviorKind): void {
    a.behaviorKind = kind;
    this.markDirty();
  }

  behaviorBadge(a: EditAction): string {
    if (a.behaviorKind === "http") return `HTTP ${a.method}`;
    if (a.behaviorKind === "code") return `Code · ${a.language}`;
    if (a.behaviorKind === "crypto") return `Crypto · ${a.cryptoOp}`;
    return "Adapter";
  }

  addParam(a: EditAction): void {
    a.paramRows.push({
      name: "",
      label: "",
      type: "string",
      options: "",
      required: false,
      default: "",
    });
    this.markDirty();
  }
  removeParam(a: EditAction, i: number): void {
    a.paramRows.splice(i, 1);
    this.markDirty();
  }
  addHeader(a: EditAction): void {
    a.headerRows.push({ name: "", value: "" });
    this.markDirty();
  }
  removeHeader(a: EditAction, i: number): void {
    a.headerRows.splice(i, 1);
    this.markDirty();
  }

  // -- conversion ----------------------------------------------------------

  private toEdit(t: ManagedTarget): EditTarget {
    return {
      target: t.target,
      label: t.label,
      category: t.category,
      color: t.color,
      description: t.description ?? "",
      icon: t.icon ?? "",
      builtin: t.builtin,
      overridden: t.overridden,
      hidden: t.hidden,
      isNew: false,
      actions: (t.actions ?? []).map((a, i) => {
        const b = a.behavior;
        const tpl = b?.template ?? {};
        // Prefer explicit typed params; fall back to deriving string params
        // from payload_hint so older catalog entries still get editable rows.
        const paramRows: EditParam[] = (a.params ?? []).length
          ? (a.params ?? []).map((p) => ({
              name: p.name,
              label: p.label ?? "",
              type: (p.type ?? "string") as EditParam["type"],
              options: (p.options ?? []).join(", "),
              required: !!p.required,
              default:
                p.default == null
                  ? ""
                  : typeof p.default === "object"
                    ? JSON.stringify(p.default)
                    : String(p.default),
            }))
          : Object.keys(a.payload_hint ?? {}).map((k) => ({
              name: k,
              label: "",
              type: "string" as const,
              options: "",
              required: false,
              default: "",
            }));
        const { operation: cryptoOp, ...cryptoTemplate } = tpl as Record<
          string,
          unknown
        >;
        return {
          name: a.name,
          label: a.label,
          paramRows,
          responseText: (a.response_fields ?? []).join(", "),
          behaviorKind: (b?.kind ?? "adapter") as StepBehaviorKind,
          method: tpl.method ?? "GET",
          url: tpl.url ?? "",
          headerRows: tpl.headers ?? [],
          body: tpl.body ?? "",
          language: tpl.language ?? "python",
          code: tpl.code ?? "return {}",
          cryptoOp: (cryptoOp as CryptoOperation) ?? "pin_block_encode",
          cryptoTemplate,
          open: i === 0,
        };
      }),
    };
  }

  private toSpec(d: EditTarget): TargetSpec {
    return {
      target: d.target.trim(),
      label: d.label.trim() || d.target.trim(),
      category: d.category.trim() || "Custom",
      color: d.color || "#7ee787",
      description: d.description.trim(),
      icon: d.icon.trim(),
      custom: true,
      actions: d.actions.map((a) => {
        const params = a.paramRows
          .filter((r) => r.name.trim())
          .map((r) => {
            const opts = r.options
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean);
            let def: unknown =
              r.default.trim() === "" ? null : r.default.trim();
            if (def != null) {
              if (r.type === "number") {
                const n = Number(def);
                if (!Number.isNaN(n)) def = n;
              } else if (r.type === "boolean") {
                def = def === "true";
              } else if (r.type === "json") {
                try {
                  def = JSON.parse(def as string);
                } catch {
                  /* keep as string if not valid JSON */
                }
              }
            }
            return {
              name: r.name.trim(),
              label: r.label.trim(),
              type: r.type,
              options: r.type === "enum" ? opts : [],
              required: r.required,
              default: def,
              placeholder: "",
            };
          });
        return {
          name: a.name.trim(),
          label: a.label.trim() || a.name.trim(),
          // Keep payload_hint in sync (names → type) for back-compat / refs.
          payload_hint: Object.fromEntries(params.map((p) => [p.name, p.type])),
          params,
          response_fields: a.responseText
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
          behavior:
            a.behaviorKind === "adapter"
              ? null
              : a.behaviorKind === "http"
                ? {
                    kind: "http" as const,
                    template: {
                      method: a.method as HttpMethod,
                      url: a.url,
                      sendHeaders: a.headerRows.length > 0,
                      headers: a.headerRows,
                      sendBody: !!a.body.trim(),
                      contentType: "json" as const,
                      body: a.body,
                    },
                  }
                : a.behaviorKind === "crypto"
                  ? {
                      kind: "crypto" as const,
                      template: {
                        ...a.cryptoTemplate,
                        operation: a.cryptoOp,
                      } as NodeConfig,
                    }
                  : {
                      kind: "code" as const,
                      template: { language: a.language, code: a.code },
                    },
        };
      }),
    };
  }

  // -- persistence ---------------------------------------------------------

  save(): void {
    const d = this.draft();
    if (!d) return;
    if (!d.target.trim()) {
      this.ui.toast("error", "Give the step type an id.");
      return;
    }
    if (!/^[a-z0-9_]+$/i.test(d.target.trim())) {
      this.ui.toast(
        "error",
        "Id may contain only letters, digits and underscores.",
      );
      return;
    }
    if (!d.actions.length || d.actions.some((a) => !a.name.trim())) {
      this.ui.toast("error", "Every action needs a name.");
      return;
    }
    this.saving.set(true);
    const id = d.target.trim();
    this.api.saveCatalogTarget(this.toSpec(d)).subscribe({
      next: () => {
        this.saving.set(false);
        this.dirty.set(false);
        this.ui.toast("success", `Saved step type “${d.label || d.target}”`);
        this.reload(id);
      },
      error: (err) => {
        this.saving.set(false);
        this.ui.toast("error", err?.error?.detail || "Save failed.");
      },
    });
  }

  async deleteOrHide(t: EditTarget): Promise<void> {
    const verb = t.builtin ? "Hide" : "Delete";
    const ok = await this.ui.confirm(
      t.builtin
        ? `Hide built-in step “${t.label}” from the palette? You can restore it later.`
        : `Delete custom step “${t.label}”? This cannot be undone.`,
      { title: `${verb} step type`, confirmLabel: verb },
    );
    if (!ok) return;
    this.api.deleteCatalogTarget(t.target).subscribe({
      next: () => {
        this.ui.toast("success", `${verb}d “${t.label || t.target}”`);
        this.draft.set(null);
        this.dirty.set(false);
        this.reload();
      },
      error: () => this.ui.toast("error", `${verb} failed.`),
    });
  }

  restore(target: string): void {
    this.api.restoreCatalogTarget(target).subscribe({
      next: () => {
        this.ui.toast("success", "Restored to the built-in default.");
        this.dirty.set(false);
        this.reload(target);
      },
      error: () => this.ui.toast("error", "Restore failed."),
    });
  }
}
