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
import {
  BinRange,
  CardPool,
  CardRecord,
  KeyType,
  KeyView,
  TerminalPool,
  TestDataService,
} from "./test-data.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";

type Tab = "cards" | "bins" | "terminals" | "keys";

interface CardPoolDraft {
  name: string;
  description: string;
  text: string; // newline-separated cards
  isNew: boolean;
  original?: string;
}
interface TerminalPoolDraft {
  name: string;
  description: string;
  text: string;
  isNew: boolean;
  original?: string;
}
interface BinDraft {
  name: string;
  description: string;
  bin: string;
  length: number;
  brand: string;
  isNew: boolean;
  original?: string;
}
interface KeyDraft {
  name: string;
  description: string;
  type: KeyType;
  value: string; // write-only; blank keeps existing
  isNew: boolean;
  original?: string;
  fingerprint?: string;
}

/**
 * Test Data manager — central place to define + inspect the data a payment test
 * feeds: card pools, BIN ranges, terminal pools, and crypto keys. Backed by the
 * scenario-service `/test-data/*` registry. Key material is write-only (the API
 * masks it on read), so the Keys tab shows type/length/fingerprint only.
 */
@Component({
  selector: "app-test-data",
  standalone: true,
  imports: [PageHeaderComponent, FormsModule, RouterLink],
  templateUrl: "./test-data.component.html",
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./test-data.component.scss",
})
export class TestDataComponent implements OnInit {
  readonly theme = inject(ThemeService);
  readonly td = inject(TestDataService);
  private readonly ui = inject(UiService);

  readonly tab = signal<Tab>("cards");
  readonly keyTypes: KeyType[] = [
    "bdk",
    "ipek",
    "pvk",
    "zmk",
    "tmk",
    "generic",
  ];

  readonly cardDraft = signal<CardPoolDraft | null>(null);
  readonly termDraft = signal<TerminalPoolDraft | null>(null);
  readonly binDraft = signal<BinDraft | null>(null);
  readonly keyDraft = signal<KeyDraft | null>(null);

  readonly genResult = signal<string[] | null>(null);
  readonly genCount = signal(10);
  readonly genPoolName = signal("");

  ngOnInit(): void {
    this.td.reloadAll();
  }

  setTab(t: Tab): void {
    this.tab.set(t);
    this.genResult.set(null);
  }

  // -- card pools ---------------------------------------------------------

  newCard(): void {
    this.cardDraft.set({ name: "", description: "", text: "", isNew: true });
  }
  editCard(p: CardPool): void {
    this.cardDraft.set({
      name: p.name,
      description: p.description,
      text: p.cards.map((c) => this.cardToLine(c)).join("\n"),
      isNew: false,
      original: p.name,
    });
  }
  saveCard(): void {
    const d = this.cardDraft();
    if (!d) return;
    if (!d.name.trim()) return this.ui.toast("error", "Give the pool a name.");
    const cards = this.parseCards(d.text);
    this.td
      .saveCardPool(
        { name: d.name, description: d.description, cards },
        d.original,
      )
      .subscribe({
        next: () => {
          this.ui.toast("success", `Saved card pool “${d.name}”`);
          this.cardDraft.set(null);
        },
        error: () => this.ui.toast("error", "Save failed."),
      });
  }
  async deleteCard(p: CardPool): Promise<void> {
    if (
      !(await this.ui.confirm(`Delete card pool “${p.name}”?`, {
        danger: true,
      }))
    )
      return;
    this.td.removeCardPool(p.name).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted “${p.name}”`);
        if (this.cardDraft()?.original === p.name) this.cardDraft.set(null);
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }

  // -- terminal pools -----------------------------------------------------

  newTerm(): void {
    this.termDraft.set({ name: "", description: "", text: "", isNew: true });
  }
  editTerm(p: TerminalPool): void {
    this.termDraft.set({
      name: p.name,
      description: p.description,
      text: p.terminals.join("\n"),
      isNew: false,
      original: p.name,
    });
  }
  saveTerm(): void {
    const d = this.termDraft();
    if (!d) return;
    if (!d.name.trim()) return this.ui.toast("error", "Give the pool a name.");
    const terminals = this.lines(d.text);
    this.td
      .saveTerminalPool(
        { name: d.name, description: d.description, terminals },
        d.original,
      )
      .subscribe({
        next: () => {
          this.ui.toast("success", `Saved terminal pool “${d.name}”`);
          this.termDraft.set(null);
        },
        error: () => this.ui.toast("error", "Save failed."),
      });
  }
  async deleteTerm(p: TerminalPool): Promise<void> {
    if (
      !(await this.ui.confirm(`Delete terminal pool “${p.name}”?`, {
        danger: true,
      }))
    )
      return;
    this.td.removeTerminalPool(p.name).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted “${p.name}”`);
        if (this.termDraft()?.original === p.name) this.termDraft.set(null);
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }

  // -- BIN ranges ---------------------------------------------------------

  newBin(): void {
    this.binDraft.set({
      name: "",
      description: "",
      bin: "",
      length: 16,
      brand: "",
      isNew: true,
    });
    this.genResult.set(null);
  }
  editBin(b: BinRange): void {
    this.binDraft.set({ ...b, isNew: false, original: b.name });
    this.genResult.set(null);
    this.genPoolName.set(`${b.name}_pool`);
  }
  saveBin(): void {
    const d = this.binDraft();
    if (!d) return;
    if (!d.name.trim())
      return this.ui.toast("error", "Give the BIN range a name.");
    if (!/^\d+$/.test(d.bin))
      return this.ui.toast("error", "BIN must be numeric.");
    this.td
      .saveBinRange(
        {
          name: d.name,
          description: d.description,
          bin: d.bin,
          length: Number(d.length),
          brand: d.brand,
        },
        d.original,
      )
      .subscribe({
        next: () => {
          this.ui.toast("success", `Saved BIN range “${d.name}”`);
          this.binDraft.set(null);
        },
        error: () => this.ui.toast("error", "Save failed."),
      });
  }
  async deleteBin(b: BinRange): Promise<void> {
    if (
      !(await this.ui.confirm(`Delete BIN range “${b.name}”?`, {
        danger: true,
      }))
    )
      return;
    this.td.removeBinRange(b.name).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted “${b.name}”`);
        if (this.binDraft()?.original === b.name) this.binDraft.set(null);
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }
  generate(saveAsPool: boolean): void {
    const d = this.binDraft();
    if (!d || d.isNew) return;
    const pool = saveAsPool ? this.genPoolName().trim() : undefined;
    this.td.generatePans(d.name, this.genCount(), pool || undefined).subscribe({
      next: (r) => {
        this.genResult.set(r.pans);
        if (r.saved_pool)
          this.ui.toast(
            "success",
            `Saved ${r.pans.length} PANs to “${r.saved_pool}”`,
          );
      },
      error: () => this.ui.toast("error", "Generate failed."),
    });
  }

  // -- keys ---------------------------------------------------------------

  newKey(): void {
    this.keyDraft.set({
      name: "",
      description: "",
      type: "generic",
      value: "",
      isNew: true,
    });
  }
  editKey(k: KeyView): void {
    this.keyDraft.set({
      name: k.name,
      description: k.description,
      type: (k.type as KeyType) ?? "generic",
      value: "",
      isNew: false,
      original: k.name,
      fingerprint: k.fingerprint,
    });
  }
  saveKey(): void {
    const d = this.keyDraft();
    if (!d) return;
    if (!d.name.trim()) return this.ui.toast("error", "Give the key a name.");
    const v = d.value.replace(/\s+/g, "");
    if (v && (v.length % 2 !== 0 || !/^[0-9a-fA-F]+$/.test(v)))
      return this.ui.toast("error", "Key value must be valid hex.");
    if (d.isNew && !v) return this.ui.toast("error", "Paste the key material.");
    this.td
      .saveKey(
        d.name,
        { description: d.description, type: d.type, value: v },
        d.original,
      )
      .subscribe({
        next: () => {
          this.ui.toast("success", `Saved key “${d.name}”`);
          this.keyDraft.set(null);
        },
        error: () => this.ui.toast("error", "Save failed."),
      });
  }
  async deleteKey(k: KeyView): Promise<void> {
    if (!(await this.ui.confirm(`Delete key “${k.name}”?`, { danger: true })))
      return;
    this.td.removeKey(k.name).subscribe({
      next: () => {
        this.ui.toast("success", `Deleted “${k.name}”`);
        if (this.keyDraft()?.original === k.name) this.keyDraft.set(null);
      },
      error: () => this.ui.toast("error", "Delete failed."),
    });
  }

  // -- helpers ------------------------------------------------------------

  private lines(text: string): string[] {
    return text
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }

  countLines(text: string): number {
    return this.cardLines(text).length;
  }

  /** Split card text into one entry per line (commas only split bare PANs, so
   *  JSON records — which contain commas — stay on their own line). */
  private cardLines(text: string): string[] {
    return text
      .split("\n")
      .flatMap((line) =>
        line.trim().startsWith("{") ? [line] : line.split(","),
      )
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
  }

  /** Render one pool entry as an editable line: a bare PAN stays a PAN; a
   *  structured record is shown as compact JSON so it round-trips losslessly. */
  private cardToLine(card: string | CardRecord): string {
    if (typeof card === "string") return card;
    // a record with only a PAN is shown as the bare PAN for readability
    const hasExtra = card.expiry || card.cvv || card.type || card.track2;
    return !hasExtra && card.pan ? card.pan : JSON.stringify(card);
  }

  /** Parse the textarea back into card entries: a ``{...}`` line is a JSON
   *  record, anything else is a bare PAN string. */
  private parseCards(text: string): (string | CardRecord)[] {
    return this.cardLines(text).map((line) => {
      if (line.startsWith("{")) {
        try {
          return JSON.parse(line) as CardRecord;
        } catch {
          return line; // malformed JSON → keep as a literal token
        }
      }
      return line;
    });
  }
}
