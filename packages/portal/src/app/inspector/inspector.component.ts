import {
  Component,
  OnInit,
  inject,
  signal,
  ChangeDetectionStrategy,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import { ScenarioApiService } from "../constructor/scenario-api.service";
import {
  IsoAnalysis,
  IsoBuildResult,
  IsoDiff,
  IsoTlvNode,
  MessageFormat,
} from "../constructor/scenario.models";
import { ThemeService } from "../shared/theme.service";
import { UiService } from "../shared/ui.service";
import { IconComponent } from "../shared/ui/icon.component";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

interface KV {
  key: string;
  value: string;
}

interface FlatTlv {
  node: IsoTlvNode;
  depth: number;
}

const SAMPLE =
  "0200723800000A8080001641111111111111110000000000000100000203040506" +
  "000001020304020300000000000100TERM0001978";

/**
 * ISO 8583 Inspector — analyze a wire message (MTI, bitmap, per-DE values with
 * validation, EMV BER-TLV for DE 55) and build/validate a message field by
 * field. Backed by the scenario service's analyzer endpoints.
 */
@Component({
  selector: "app-inspector",
  standalone: true,
  imports: [PageHeaderComponent, FormsModule, IconComponent],
  templateUrl: "./inspector.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./inspector.component.scss",
})
export class InspectorComponent implements OnInit {
  readonly theme = inject(ThemeService);
  private readonly api = inject(ScenarioApiService);
  private readonly ui = inject(UiService);

  readonly mode = signal<"analyze" | "build" | "diff">("analyze");

  // message-format field table (default 1987 when none selected)
  readonly formats = signal<MessageFormat[]>([]);
  readonly formatId = signal<string>("");

  ngOnInit(): void {
    this.api.formats("iso8583").subscribe({
      next: (f) => this.formats.set(f),
      error: () => this.formats.set([]),
    });
  }

  get formatModel(): string {
    return this.formatId();
  }
  set formatModel(v: string) {
    this.formatId.set(v);
  }

  /** DE table from the selected Message Format, or undefined for the default. */
  private selectedFields(): Record<string, unknown> | undefined {
    const f = this.formats().find((x) => x.id === this.formatId());
    const fields = f?.definition?.["fields"];
    return fields && typeof fields === "object"
      ? (fields as Record<string, unknown>)
      : undefined;
  }

  // analyze
  readonly message = signal<string>(SAMPLE);
  readonly analysis = signal<IsoAnalysis | null>(null);
  readonly analyzing = signal(false);

  // build
  readonly mti = signal<string>("0200");
  readonly rows = signal<KV[]>([
    { key: "2", value: "4111111111111111" },
    { key: "4", value: "000000010000" },
    { key: "11", value: "000123" },
  ]);
  readonly buildResult = signal<IsoBuildResult | null>(null);

  // ngModel accessors over the signals
  get messageModel(): string {
    return this.message();
  }
  set messageModel(v: string) {
    this.message.set(v);
  }
  get mtiModel(): string {
    return this.mti();
  }
  set mtiModel(v: string) {
    this.mti.set(v);
  }

  setMode(m: "analyze" | "build" | "diff"): void {
    this.mode.set(m);
  }

  // -- diff ----------------------------------------------------------------

  readonly msgA = signal<string>(SAMPLE);
  readonly msgB = signal<string>("");
  readonly diff = signal<IsoDiff | null>(null);
  readonly diffing = signal(false);
  get msgAModel(): string {
    return this.msgA();
  }
  set msgAModel(v: string) {
    this.msgA.set(v);
  }
  get msgBModel(): string {
    return this.msgB();
  }
  set msgBModel(v: string) {
    this.msgB.set(v);
  }

  diffMessages(): void {
    const a = this.msgA().trim(),
      b = this.msgB().trim();
    if (!a || !b) {
      this.ui.toast("error", "Paste both messages to diff.");
      return;
    }
    this.diffing.set(true);
    this.api.diffIso8583(a, b, this.selectedFields()).subscribe({
      next: (d) => {
        this.diffing.set(false);
        this.diff.set(d);
      },
      error: () => {
        this.diffing.set(false);
        this.ui.toast(
          "error",
          "Diff failed — is the scenario service running?",
        );
      },
    });
  }

  // -- DE 55 TLV builder ---------------------------------------------------

  readonly tlvRows = signal<KV[]>([
    { key: "9F26", value: "1122334455667788" },
    { key: "9F36", value: "001C" },
  ]);
  readonly tlvHex = signal<string>("");

  addTlvRow(): void {
    this.tlvRows.update((r) => [...r, { key: "", value: "" }]);
  }
  removeTlvRow(i: number): void {
    this.tlvRows.update((r) => r.filter((_, idx) => idx !== i));
  }

  buildTlv(): void {
    const nodes = this.tlvRows()
      .filter((r) => r.key.trim())
      .map((r) => ({ tag: r.key.trim().toUpperCase(), value: r.value.trim() }));
    this.api.buildTlv(nodes).subscribe({
      next: (res) => this.tlvHex.set(res.hex),
      error: () =>
        this.ui.toast("error", "TLV build failed (check tags are hex)."),
    });
  }

  /** Drop the built TLV hex into a DE 55 row in the Build tab. */
  useTlvInDe55(): void {
    const hex = this.tlvHex();
    if (!hex) return;
    this.rows.update((rows) => {
      const i = rows.findIndex((r) => r.key.trim() === "55");
      if (i >= 0) {
        const copy = [...rows];
        copy[i] = { key: "55", value: hex };
        return copy;
      }
      return [...rows, { key: "55", value: hex }];
    });
    this.ui.toast("success", "Set DE 55 to the built TLV.");
  }

  // -- analyze -------------------------------------------------------------

  analyze(): void {
    const msg = this.message().trim();
    if (!msg) {
      this.ui.toast("error", "Paste a message to analyze.");
      return;
    }
    this.analyzing.set(true);
    this.api.analyzeIso8583(msg, this.selectedFields()).subscribe({
      next: (a) => {
        this.analyzing.set(false);
        this.analysis.set(a);
      },
      error: () => {
        this.analyzing.set(false);
        this.ui.toast(
          "error",
          "Analyze failed — is the scenario service running?",
        );
      },
    });
  }

  /** Flatten an EMV TLV tree to indented rows for display. */
  flatTlv(
    nodes: IsoTlvNode[] | undefined,
    depth = 0,
    acc: FlatTlv[] = [],
  ): FlatTlv[] {
    for (const n of nodes ?? []) {
      acc.push({ node: n, depth });
      if (n.children) this.flatTlv(n.children, depth + 1, acc);
    }
    return acc;
  }

  // -- build (format-aware) ------------------------------------------------

  /** Picker model for adding a known DE from the selected format. */
  pickDe = "";

  /** The selected format's DE specs, or undefined for the default table. */
  private specs():
    | Record<
        string,
        { name?: string; len_type?: string; length?: number; type?: string }
      >
    | undefined {
    return this.selectedFields() as
      | Record<
          string,
          { name?: string; len_type?: string; length?: number; type?: string }
        >
      | undefined;
  }

  /** DEs offered by the selected format (sorted numerically) for the picker. */
  availableFields(): { de: string; name: string }[] {
    const s = this.specs();
    if (!s) return [];
    return Object.entries(s)
      .map(([de, spec]) => ({ de, name: spec?.name ?? "" }))
      .sort((a, b) => +a.de - +b.de);
  }

  /** Friendly name for a DE from the selected format (empty if unknown). */
  fieldName(de: string): string {
    return this.specs()?.[de?.trim()]?.name ?? "";
  }

  /** Live per-field validation mirroring the server's rules (only when a
   *  format gives us a spec for the DE). Returns an error string, or null. */
  rowError(row: KV): string | null {
    const spec = this.specs()?.[row.key?.trim()];
    if (!spec) return null; // unknown DE / no format -> nothing to check inline
    const v = row.value ?? "";
    const lt = spec.len_type ?? "fixed";
    const len = Number(spec.length ?? 0);
    const type = (spec.type ?? "").toLowerCase();
    if (lt === "fixed" && len && v.length !== len)
      return `expected ${len} chars, got ${v.length}`;
    if ((lt === "llvar" || lt === "lllvar") && len && v.length > len)
      return `exceeds max ${len}`;
    if (type === "n" && v && !/^[0-9]+$/.test(v)) return "must be numeric";
    if (type === "b" && v && !/^[0-9a-fA-F]*$/.test(v))
      return "must be hexadecimal";
    return null;
  }

  addRow(): void {
    this.rows.update((r) => [...r, { key: "", value: "" }]);
  }

  /** Add a row for a DE chosen from the format picker (no duplicates). */
  addField(de: string): void {
    this.pickDe = "";
    if (!de) return;
    if (this.rows().some((r) => r.key === de)) return;
    this.rows.update((r) => [...r, { key: de, value: "" }]);
  }

  removeRow(i: number): void {
    this.rows.update((r) => r.filter((_, idx) => idx !== i));
  }

  build(): void {
    const values: Record<string, string> = {};
    for (const { key, value } of this.rows()) {
      if (key.trim()) values[key.trim()] = value;
    }
    this.api
      .buildIso8583(this.mti().trim(), values, this.selectedFields())
      .subscribe({
        next: (res) => this.buildResult.set(res),
        error: () =>
          this.ui.toast(
            "error",
            "Build failed — is the scenario service running?",
          ),
      });
  }

  /** Send a freshly built message over to the Analyze tab. */
  analyzeBuilt(): void {
    const msg = this.buildResult()?.message;
    if (!msg) return;
    this.message.set(msg);
    this.mode.set("analyze");
    this.analyze();
  }

  async copy(text: string | null | undefined): Promise<void> {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      this.ui.toast("success", "Copied to clipboard.");
    } catch {
      this.ui.toast("error", "Clipboard unavailable.");
    }
  }
}
