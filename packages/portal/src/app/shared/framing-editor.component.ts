import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from "@angular/core";
import { FormsModule } from "@angular/forms";

import { FramingConfig } from "../connections/connection.models";
import { IconComponent } from "./ui/icon.component";

/** One sample frame, rendered by the wire diagram. */
export interface FramingPreview {
  n: number;
  prefix: { hex: string; char?: string }[];
  tpdu: string[];
  body: number;
  counted: number;
  formula: string;
  overflow: boolean;
  total: number;
}

export interface FramingPreset {
  key: string;
  label: string;
  hint: string;
  set: Partial<FramingConfig>;
}

/** Common framings, one click each. */
export const FRAMING_PRESETS: FramingPreset[] = [
  {
    key: "iso",
    label: "2-byte binary",
    hint: "ISO 8583 default: big-endian, no TPDU",
    set: {
      lengthPrefixBytes: 2,
      lengthEncoding: "binary",
      byteOrder: "big",
      lengthIncludesPrefix: false,
      lengthIncludesHeader: true,
      tpduBytes: 0,
      tpduOutboundHex: "",
    },
  },
  {
    key: "tpdu",
    label: "2-byte + TPDU",
    hint: "POS terminals: 60 00 00 00 00 before the message",
    set: {
      lengthPrefixBytes: 2,
      lengthEncoding: "binary",
      byteOrder: "big",
      lengthIncludesPrefix: false,
      lengthIncludesHeader: true,
      tpduBytes: 5,
      tpduOutboundHex: "6000000000",
    },
  },
  {
    key: "ascii4",
    label: "4-digit ASCII",
    hint: "zero-padded digits, e.g. 0043",
    set: {
      lengthPrefixBytes: 4,
      lengthEncoding: "ascii",
      lengthIncludesPrefix: false,
      lengthIncludesHeader: true,
      tpduBytes: 0,
      tpduOutboundHex: "",
    },
  },
  {
    key: "le",
    label: "2-byte little-endian",
    hint: "low byte first, some host systems",
    set: {
      lengthPrefixBytes: 2,
      lengthEncoding: "binary",
      byteOrder: "little",
      lengthIncludesPrefix: false,
      lengthIncludesHeader: true,
      tpduBytes: 0,
      tpduOutboundHex: "",
    },
  },
];

/** One-line state of a framing, for a collapsed section or a list. */
export function framingSummary(f: FramingConfig): string {
  const width =
    f.lengthEncoding === "ascii"
      ? `${f.lengthPrefixBytes}-digit ASCII`
      : `${f.lengthPrefixBytes}-byte ${f.byteOrder}-endian`;
  const parts = [width];
  if (f.lengthIncludesPrefix) parts.push("counts prefix");
  if (!f.lengthIncludesHeader) parts.push("excludes TPDU");
  if (f.tpduBytes || f.tpduOutboundHex) parts.push("TPDU");
  if (f.encoding && f.encoding !== "ascii") parts.push(f.encoding);
  return parts.join(" · ");
}

/** The frame a 43-byte sample message gets on the wire, byte by byte. */
export function framingPreview(f: FramingConfig): FramingPreview {
  const n = Math.max(1, Math.min(8, Number(f.lengthPrefixBytes) || 2));
  const hex = (f.tpduOutboundHex || "")
    .replace(/[^0-9a-fA-F]/g, "")
    .toUpperCase();
  const tpdu = hex.length % 2 === 0 ? (hex.match(/../g) ?? []) : [];
  const body = 43;
  const counted =
    (f.lengthIncludesHeader ? body + tpdu.length : body) +
    (f.lengthIncludesPrefix ? n : 0);
  const terms = [`${body} body`];
  if (f.lengthIncludesHeader && tpdu.length) terms.push(`${tpdu.length} TPDU`);
  if (f.lengthIncludesPrefix) terms.push(`${n} prefix`);
  let prefix: { hex: string; char?: string }[];
  let overflow = false;
  if (f.lengthEncoding === "ascii") {
    overflow = counted > 10 ** n - 1;
    const digits = String(Math.min(counted, 10 ** n - 1)).padStart(n, "0");
    prefix = [...digits].map((c) => ({
      char: c,
      hex: c.charCodeAt(0).toString(16).toUpperCase(),
    }));
  } else {
    overflow = counted > 256 ** n - 1;
    const bytes =
      counted
        .toString(16)
        .toUpperCase()
        .padStart(n * 2, "0")
        .match(/../g) ?? [];
    prefix = (f.byteOrder === "little" ? bytes.reverse() : bytes).map((h) => ({
      hex: h,
    }));
  }
  return {
    n,
    prefix,
    tpdu,
    body,
    counted,
    formula: terms.join(" + "),
    overflow,
    total: n + tpdu.length + body,
  };
}

/** Combinations that cannot work, said before the first stalled read. */
export function framingIssues(
  f: FramingConfig,
  mode?: string | null,
): string[] {
  const out: string[] = [];
  const n = Number(f.lengthPrefixBytes);
  if (!(n >= 1)) out.push("The length prefix needs at least one byte.");
  if (f.lengthEncoding === "ascii" && n >= 1 && n <= 2) {
    out.push(
      `A ${n}-digit ASCII prefix caps a message at ${10 ** n - 1} bytes.`,
    );
  }
  const hex = (f.tpduOutboundHex || "").trim();
  if (hex && (!/^[0-9a-fA-F]*$/.test(hex) || hex.length % 2 === 1)) {
    out.push(
      "The outbound TPDU must be an even number of hex digits (e.g. 6000000000).",
    );
  }
  if (hex && !f.tpduBytes) {
    out.push(
      "A TPDU is sent but none is stripped on the way in: replies will carry it as message bytes.",
    );
  }
  if (f.tpduBytes && !hex && mode !== "inbound") {
    out.push(
      "Inbound TPDU bytes are stripped but nothing is sent; fine only if the host does not expect one.",
    );
  }
  return out;
}

/**
 * The framing editor: presets, a live wire diagram of a sample frame, the
 * fields grouped with a hint on each, and warnings for combinations that
 * cannot work. Mutates the `framing` object it is given (the way the forms
 * hold their drafts) and emits `changed` after every edit. Used by the
 * connection form and by the simulator editor (which reads and writes the
 * `framing` key of the responder config JSON).
 */
@Component({
  selector: "pp-framing-editor",
  standalone: true,
  imports: [FormsModule, IconComponent],
  changeDetection: ChangeDetectionStrategy.Default,
  template: `
    @if (framing; as f) {
      <p class="muted fr__lead">
        How message boundaries are marked on the TCP stream. Both ends must
        agree: a mismatch shows up as a read that never completes or a message
        that will not decode.
      </p>

      <div class="fr__presets">
        <span class="muted">Presets</span>
        @for (p of presets; track p.key) {
          <button
            type="button"
            class="fr__preset"
            [class.active]="presetActive(p.key)"
            [title]="p.hint"
            (click)="applyPreset(p.key)"
          >
            {{ p.label }}
          </button>
        }
      </div>

      @if (preview(); as w) {
        <div class="wire" [class.wire--bad]="w.overflow">
          <div class="wire__seg wire__seg--prefix">
            <span class="wire__cap"
              >length prefix · {{ w.n }}
              {{ f.lengthEncoding === "ascii" ? "digit" : "byte"
              }}{{ w.n === 1 ? "" : "s" }}</span
            >
            <span class="wire__bytes">
              @for (c of w.prefix; track $index) {
                <span class="wire__byte">
                  @if (c.char) {
                    <span class="wire__char">{{ c.char }}</span>
                  }
                  <span class="wire__hex">{{ c.hex }}</span>
                </span>
              }
            </span>
          </div>
          @if (w.tpdu.length) {
            <div
              class="wire__seg wire__seg--tpdu"
              [class.wire__seg--counted]="f.lengthIncludesHeader"
            >
              <span class="wire__cap">TPDU · {{ w.tpdu.length }} bytes</span>
              <span class="wire__bytes">
                @for (h of w.tpdu; track $index) {
                  <span class="wire__byte"
                    ><span class="wire__hex">{{ h }}</span></span
                  >
                }
              </span>
            </div>
          }
          <div class="wire__seg wire__seg--body wire__seg--counted">
            <span class="wire__cap">message · {{ w.body }} bytes</span>
            <span class="wire__bytes">
              <span class="wire__byte"><span class="wire__hex">30</span></span>
              <span class="wire__byte"><span class="wire__hex">32</span></span>
              <span class="wire__byte"><span class="wire__hex">30</span></span>
              <span class="wire__byte"><span class="wire__hex">30</span></span>
              <span class="wire__more">… {{ w.body - 4 }} more</span>
            </span>
          </div>
        </div>
        <p class="wire__formula">
          <span class="wire__k">length</span> = {{ w.formula }} =
          <strong>{{ w.counted }}</strong>
          @if (w.overflow) {
            <span class="wire__warn">
              does not fit a {{ w.n }}-{{
                f.lengthEncoding === "ascii" ? "digit" : "byte"
              }}
              prefix
            </span>
          }
          <span class="muted">· {{ w.total }} bytes on the wire</span>
        </p>
      }

      <h4 class="fr__h">
        <pp-icon name="package" [size]="13" /> Length prefix
      </h4>
      <div class="grid2">
        <label class="fld">
          <span
            >Width
            <small class="muted">(bytes, or digits for ASCII)</small></span
          >
          <input
            type="number"
            min="1"
            max="8"
            [(ngModel)]="f.lengthPrefixBytes"
            (ngModelChange)="emit()"
          />
        </label>
        <label class="fld">
          <span
            >Length encoding
            <small class="muted">(how the number is written)</small></span
          >
          <select [(ngModel)]="f.lengthEncoding" (ngModelChange)="emit()">
            <option value="binary">binary integer (00 2B)</option>
            <option value="ascii">ASCII digits (0043)</option>
          </select>
        </label>
        <label class="fld">
          <span
            >Byte order
            <small class="muted">{{
              f.lengthEncoding === "ascii"
                ? "(not used for ASCII digits)"
                : "(binary integers only)"
            }}</small></span
          >
          <select
            [(ngModel)]="f.byteOrder"
            (ngModelChange)="emit()"
            [disabled]="f.lengthEncoding === 'ascii'"
          >
            <option value="big">big-endian (high byte first)</option>
            <option value="little">little-endian (low byte first)</option>
          </select>
        </label>
        <div class="fld fld--stack">
          <span>What the length counts</span>
          <label class="chkrow">
            <input
              type="checkbox"
              [(ngModel)]="f.lengthIncludesHeader"
              (ngModelChange)="emit()"
            />
            <span
              >the TPDU header
              <small class="muted">(when one is sent)</small></span
            >
          </label>
          <label class="chkrow">
            <input
              type="checkbox"
              [(ngModel)]="f.lengthIncludesPrefix"
              (ngModelChange)="emit()"
            />
            <span
              >its own prefix bytes <small class="muted">(rare)</small></span
            >
          </label>
        </div>
      </div>

      <h4 class="fr__h">
        <pp-icon name="layers" [size]="13" /> TPDU header
        <small class="muted"
          >(optional transport header before the message)</small
        >
      </h4>
      <div class="grid2">
        <label class="fld">
          <span
            >Outbound TPDU
            <small class="muted">(hex, sent before every message)</small></span
          >
          <input
            type="text"
            class="mono"
            [(ngModel)]="f.tpduOutboundHex"
            (ngModelChange)="emit()"
            placeholder="6000000000"
          />
        </label>
        <label class="fld">
          <span
            >Inbound TPDU bytes
            <small class="muted">(stripped from every reply)</small></span
          >
          <input
            type="number"
            min="0"
            [(ngModel)]="f.tpduBytes"
            (ngModelChange)="emit()"
          />
        </label>
      </div>

      <h4 class="fr__h"><pp-icon name="format" [size]="13" /> Message body</h4>
      <div class="grid2">
        <label class="fld">
          <span
            >Text encoding
            <small class="muted"
              >(of the message bytes, ascii unless the host says
              otherwise)</small
            ></span
          >
          <input
            type="text"
            class="mono"
            [(ngModel)]="f.encoding"
            (ngModelChange)="emit()"
            placeholder="ascii"
          />
        </label>
      </div>

      @if (issues(); as issues) {
        @if (issues.length) {
          <ul class="fr__issues">
            @for (i of issues; track i) {
              <li><pp-icon name="info" [size]="13" /> {{ i }}</li>
            }
          </ul>
        }
      }
    }
  `,
  styleUrl: "./framing-editor.component.scss",
})
export class FramingEditorComponent {
  /** The framing draft, edited in place. */
  @Input({ required: true }) framing!: FramingConfig;
  /** The connection's direction, for the TPDU warnings (null = unknown). */
  @Input() mode: "inbound" | "outbound" | null = null;
  @Output() changed = new EventEmitter<void>();

  readonly presets = FRAMING_PRESETS;

  emit(): void {
    this.changed.emit();
  }

  applyPreset(key: string): void {
    const p = this.presets.find((x) => x.key === key);
    if (!p) return;
    Object.assign(this.framing, p.set);
    this.emit();
  }

  presetActive(key: string): boolean {
    const p = this.presets.find((x) => x.key === key);
    if (!p) return false;
    const f = this.framing as unknown as Record<string, unknown>;
    return Object.entries(p.set).every(
      ([k, v]) => String(f[k] ?? "") === String(v),
    );
  }

  preview(): FramingPreview {
    return framingPreview(this.framing);
  }

  issues(): string[] {
    return framingIssues(this.framing, this.mode);
  }
}
