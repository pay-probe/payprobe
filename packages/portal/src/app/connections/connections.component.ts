import {
  Component,
  OnInit,
  computed,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { IconComponent } from "../shared/ui/icon.component";
import {
  Connection,
  blankConnection,
  toAdapterConfig,
} from "./connection.models";
import {
  ADAPTER_KINDS,
  PARAM_TYPES,
  PARAM_TYPE_LABELS,
  ParamRow,
  coerceParamValue,
  inferParamType,
} from "./parameter-types";

/** True when the connection has a usable address for its adapter type. */
function hasAddress(d: Connection): boolean {
  if (d.extends) return true;
  if (d.adapter === "grpc") return !!(d.grpc.target.trim() || d.host.trim());
  // NATS: a broker address via host or an explicit servers[] list.
  if (d.adapter === "nats") return !!(d.host.trim() || d.nats.servers.trim());
  return !!d.host.trim();
}
import { ConnectionsService } from "./connections.service";
import { EnvironmentsService } from "../environments/environments.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

/**
 * Adapter Connections builder — define named connections (several switches, a
 * primary/backup HSM, …), each with its own protocol and config. Persists in
 * the browser and exports the worker-shaped `adapters` JSON block to drop into
 * an environment file or an Execute request.
 */
@Component({
  selector: "app-connections",
  standalone: true,
  imports: [PageHeaderComponent, FormsModule, IconComponent],
  templateUrl: "./connections.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./connections.component.scss",
})
export class ConnectionsComponent implements OnInit {
  readonly theme = inject(ThemeService);
  readonly store = inject(ConnectionsService);
  readonly envs = inject(EnvironmentsService);
  private readonly ui = inject(UiService);

  ngOnInit(): void {
    this.store.reload();
    this.envs.reload();
  }

  /** Available environments to override params for (name + label). */
  readonly environmentOptions = this.envs.environments;

  /** Per-environment override editing rows for the open draft. Stable row
   * objects so ngModel edits don't lose focus; serialised into the draft's
   * ``environmentOverrides`` map on save. Keyed by environment name/slug. */
  overrideRows: Record<string, ParamRow[]> = {};

  readonly paramTypes = PARAM_TYPES;
  readonly paramTypeLabels = PARAM_TYPE_LABELS;
  readonly adapterKinds = ADAPTER_KINDS;

  /** Registered connection names, for the ``connection``-typed value picker. */
  connectionNames(): string[] {
    return this.connections().map((c) => c.name);
  }

  /** When the type changes, an incompatible value would be confusing — clear it
   * for the reference types so the dropdown starts empty. */
  onParamTypeChange(row: ParamRow): void {
    if (row.type === "adapter" || row.type === "connection") row.value = "";
    this.markDirty();
  }

  /** Rebuild the override editor rows from a connection's stored overrides,
   * inferring each parameter's type from its value (types aren't persisted). */
  private initOverrideRows(conn: Connection): void {
    this.overrideRows = {};
    const names = this.connectionNames();
    for (const [env, ov] of Object.entries(conn.environmentOverrides ?? {})) {
      this.overrideRows[env] = Object.entries(ov).map(([key, value]) => ({
        key,
        value: String(value),
        type: inferParamType(String(value), names),
      }));
    }
  }

  /** Environments to show in the override editor: all known environments plus
   * any the connection already overrides (even if since deleted). */
  overrideEnvList(): { name: string; label: string }[] {
    const out = this.environmentOptions().map((e) => ({
      name: e.name,
      label: e.label || e.name,
    }));
    const known = new Set(out.map((e) => e.name));
    for (const env of Object.keys(this.overrideRows)) {
      if (!known.has(env)) out.push({ name: env, label: env });
    }
    return out;
  }

  rowsFor(env: string): ParamRow[] {
    return (this.overrideRows[env] ??= []);
  }

  addOverrideRow(env: string): void {
    this.rowsFor(env).push({ key: "", value: "", type: "string" });
    this.markDirty();
  }

  removeOverrideRow(env: string, i: number): void {
    this.rowsFor(env).splice(i, 1);
    this.markDirty();
  }

  /** Serialise the editing rows into the worker-shaped per-environment override
   * map (value coerced per its type; blank keys and empty environments dropped). */
  private serializeOverrides(): Record<string, Record<string, unknown>> {
    const map: Record<string, Record<string, unknown>> = {};
    for (const [env, rows] of Object.entries(this.overrideRows)) {
      const obj: Record<string, unknown> = {};
      for (const r of rows) {
        const key = r.key.trim();
        if (key) obj[key] = coerceParamValue(r.value, r.type);
      }
      if (Object.keys(obj).length) map[env] = obj;
    }
    return map;
  }

  /** Phase 2 inheritance view (read-only): the config this connection resolves to
   * under one environment — base ⊕ override. ``base`` is the env-invariant
   * worker-shaped config; the override is what this environment's rows add or
   * replace. Mirrors the orchestrator's ``_attach_connections`` merge so the
   * preview matches what actually runs once the connection-wins flip is on. */
  effectiveRows(env: string): {
    key: string;
    base: string;
    effective: string;
    overridden: boolean;
  }[] {
    const d = this.draft();
    if (!d) return [];
    const base = toAdapterConfig(d);
    const override: Record<string, unknown> = {};
    for (const r of this.rowsFor(env)) {
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

  /** Human label for the connection list — the adapter type, not the (often
   *  irrelevant) wire protocol. payShield/DB/REST/gRPC have no ISO 8583 wire, so
   *  showing "iso8583" for them was misleading. */
  connLabel(c: Connection): string {
    switch (c.adapter as string) {
      case "grpc":
        return "gRPC";
      case "http":
        return "REST / HTTP";
      case "nats":
        return "NATS";
      case "payshield":
      case "hsm_client":
        return "payShield (HSM)";
      case "db_probe_core":
        return "DB probe · core";
      case "db_probe_switch":
        return "DB probe · switch";
      default:
        return c.protocol === "header_echo" ? "header echo (HSM)" : "ISO 8583";
    }
  }

  private fmtVal(v: unknown): string {
    if (v === undefined) return "—";
    if (v !== null && typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  readonly draft = signal<Connection | null>(null);
  readonly originalName = signal<string | null>(null);
  readonly dirty = signal(false);
  readonly showExport = signal(true);
  readonly testing = signal(false);
  readonly testResult = signal<{
    ok: boolean;
    latency_ms: number;
    error: string | null;
  } | null>(null);

  readonly connections = this.store.connections;

  /** Other connection names — candidates for the `extends` dropdown. */
  readonly extendsOptions = computed(() =>
    this.connections()
      .map((i) => i.name)
      .filter((n) => n && n !== this.originalName()),
  );

  isActive(i: Connection): boolean {
    return this.originalName() === i.name && this.draft() !== null;
  }

  /** Live-probe the connection currently in the form (no save needed). */
  /** Add/remove gRPC metadata and action rows (edited in the form). */
  addMeta(): void {
    this.draft()?.grpc.metadata.push({ key: "", value: "" });
    this.markDirty();
  }
  removeMeta(idx: number): void {
    this.draft()?.grpc.metadata.splice(idx, 1);
    this.markDirty();
  }
  addAction(): void {
    this.draft()?.grpc.actions.push({ name: "", method: "" });
    this.markDirty();
  }
  removeAction(idx: number): void {
    this.draft()?.grpc.actions.splice(idx, 1);
    this.markDirty();
  }

  /** Whether a .proto file is being dragged over the drop zone (for styling). */
  readonly protoDragOver = signal(false);

  /** Add an empty .proto file row (the user pastes source into it). */
  addProtoFile(): void {
    const g = this.draft()?.grpc;
    if (!g) return;
    const n = g.protoFiles.length;
    g.protoFiles.push({
      name: n ? `service_${n}.proto` : "service.proto",
      text: "",
    });
    this.markDirty();
  }
  removeProtoFile(idx: number): void {
    this.draft()?.grpc.protoFiles.splice(idx, 1);
    this.markDirty();
  }

  /** Add files chosen via the file picker. */
  onProtoFilesPicked(event: Event): void {
    const input = event.target as HTMLInputElement;
    if (input.files) this.ingestProtoFiles(input.files);
    input.value = ""; // allow re-picking the same file
  }

  onProtoDragOver(event: DragEvent): void {
    event.preventDefault();
    this.protoDragOver.set(true);
  }
  onProtoDragLeave(): void {
    this.protoDragOver.set(false);
  }
  onProtoDrop(event: DragEvent): void {
    event.preventDefault();
    this.protoDragOver.set(false);
    if (event.dataTransfer?.files?.length)
      this.ingestProtoFiles(event.dataTransfer.files);
  }

  /** Read dropped/picked files as text and add (or replace by name) proto rows. */
  private ingestProtoFiles(files: FileList): void {
    const g = this.draft()?.grpc;
    if (!g) return;
    let added = 0;
    for (const file of Array.from(files)) {
      if (!/\.proto$/i.test(file.name)) {
        this.ui.toast("error", `Skipped “${file.name}” — only .proto files.`);
        continue;
      }
      added++;
      file.text().then((text) => {
        const existing = g.protoFiles.find((f) => f.name === file.name);
        if (existing) existing.text = text;
        else g.protoFiles.push({ name: file.name, text });
        this.markDirty();
      });
    }
    if (added) this.markDirty();
  }

  testConnection(): void {
    const d = this.draft();
    if (!d) return;
    if (!hasAddress(d)) {
      this.ui.toast(
        "error",
        d.adapter === "grpc"
          ? "Set a target (or host/port) before testing."
          : "Set a host before testing.",
      );
      return;
    }
    this.testing.set(true);
    this.testResult.set(null);
    this.store.test(d).subscribe({
      next: (res) => {
        this.testing.set(false);
        this.testResult.set(res);
        this.ui.toast(
          res.ok ? "success" : "error",
          res.ok ? `Connected (${res.latency_ms} ms)` : `Failed: ${res.error}`,
        );
      },
      error: () => {
        this.testing.set(false);
        this.testResult.set({
          ok: false,
          latency_ms: 0,
          error: "request failed",
        });
        this.ui.toast("error", "Test failed — is the orchestrator running?");
      },
    });
  }

  /** Duplicate the current connection into a new, unsaved draft. */
  clone(): void {
    const d = this.draft();
    if (!d) return;
    const copy = structuredClone(d);
    let name = `${d.name}_copy`;
    let n = 2;
    while (this.store.nameExists(name)) name = `${d.name}_copy${n++}`;
    copy.name = name;
    this.draft.set(copy);
    this.initOverrideRows(copy);
    this.originalName.set(null); // unsaved -> save() will create
    this.dirty.set(true);
    this.testResult.set(null);
  }

  newConnection(): void {
    const blank = blankConnection();
    this.draft.set(blank);
    this.initOverrideRows(blank);
    this.originalName.set(null);
    this.dirty.set(false);
  }

  edit(i: Connection): void {
    const copy = structuredClone(i);
    this.draft.set(copy);
    this.initOverrideRows(copy);
    this.originalName.set(i.name);
    this.dirty.set(false);
  }

  cancel(): void {
    this.draft.set(null);
    this.originalName.set(null);
    this.dirty.set(false);
  }

  markDirty(): void {
    this.dirty.set(true);
  }

  save(): void {
    const d = this.draft();
    if (!d) return;
    const name = d.name.trim();
    if (!name) {
      this.ui.toast("error", "Give the connection a name.");
      return;
    }
    if (this.store.nameExists(name, this.originalName() ?? undefined)) {
      this.ui.toast("error", `A connection named “${name}” already exists.`);
      return;
    }
    if (!hasAddress(d)) {
      this.ui.toast(
        "error",
        d.adapter === "grpc"
          ? "A target (or host/port) is required — or inherit one via Extends."
          : "Host is required (or inherit one via Extends).",
      );
      return;
    }
    if (d.adapter === "grpc" && !d.extends) {
      const g = d.grpc;
      if (
        g.schemaSource === "descriptor" &&
        !g.descriptorSetPath.trim() &&
        !g.descriptorSetB64.trim()
      ) {
        this.ui.toast("error", "Descriptor set source needs a path or base64.");
        return;
      }
      const hasProtoFile = g.protoFiles.some(
        (f) => f.name.trim() && f.text.trim(),
      );
      if (g.schemaSource === "proto" && !hasProtoFile && !g.protoPaths.trim()) {
        this.ui.toast(
          "error",
          "Add a .proto file (paste/drop) or list worker-host paths.",
        );
        return;
      }
    }
    d.name = name;
    d.environmentOverrides = this.serializeOverrides();
    this.store.save(d, this.originalName() ?? undefined).subscribe({
      next: () => {
        this.originalName.set(name);
        this.dirty.set(false);
        this.ui.toast("success", `Saved connection “${name}”`);
      },
      error: () =>
        this.ui.toast("error", "Save failed — is the service running?"),
    });
  }

  async remove(i: Connection): Promise<void> {
    const ok = await this.ui.confirm(`Delete connection “${i.name}”?`, {
      title: "Delete connection",
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    this.store.remove(i.name).subscribe({
      next: () => {
        if (this.originalName() === i.name) this.cancel();
        this.ui.toast("success", `Deleted “${i.name}”`);
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }

  async copyExport(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.store.adaptersJson());
      this.ui.toast("success", "Copied adapters JSON to clipboard.");
    } catch {
      this.ui.toast(
        "error",
        "Clipboard unavailable — select and copy manually.",
      );
    }
  }

  downloadExport(): void {
    const blob = new Blob([this.store.adaptersJson()], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "adapters.json";
    a.click();
    URL.revokeObjectURL(url);
  }
}
