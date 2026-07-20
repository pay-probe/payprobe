import { HttpClient } from "@angular/common/http";
import { Injectable, inject, signal } from "@angular/core";
import { Observable, tap } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

/** A structured card record (feeds ${card.pan}, ${card.expiry}, ...). */
export interface CardRecord {
  pan: string;
  expiry?: string;
  cvv?: string;
  type?: string;
  track2?: string;
}

/** A named list of PANs / card tokens (feeds ${pool.card} / ${card.*}). An
 *  entry is either a bare PAN string or a structured {@link CardRecord}. */
export interface CardPool {
  name: string;
  description: string;
  cards: (string | CardRecord)[];
}

/** A BIN + length + brand; source for generating Luhn-valid PANs. */
export interface BinRange {
  name: string;
  description: string;
  bin: string;
  length: number;
  brand: string;
}

/** A named list of terminal IDs (feeds ${pool.terminal}). */
export interface TerminalPool {
  name: string;
  description: string;
  terminals: string[];
}

/** Masked view of a stored key — material is never returned by the API. */
export interface KeyView {
  name: string;
  description: string;
  type: string;
  length: number;
  fingerprint: string;
}

export type KeyType = "bdk" | "ipek" | "pvk" | "zmk" | "tmk" | "generic";

/** Server-persisted reusable test data (scenario-service `/test-data/*`).
 *
 * Mirrors ConnectionsService: a thin HTTP client over the registry, with local
 * signal caches kept in sync by the `reload*` calls. Key material is write-only
 * — the API masks it on read, so the keys cache holds {@link KeyView}s only.
 */
@Injectable({ providedIn: "root" })
export class TestDataService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  private get base(): string {
    return `${this.cfg.scenarioApiBase}/test-data`;
  }

  readonly cardPools = signal<CardPool[]>([]);
  readonly binRanges = signal<BinRange[]>([]);
  readonly terminalPools = signal<TerminalPool[]>([]);
  readonly keys = signal<KeyView[]>([]);
  readonly loading = signal(false);

  reloadAll(): void {
    this.reloadCardPools();
    this.reloadBinRanges();
    this.reloadTerminalPools();
    this.reloadKeys();
  }

  // -- card pools ---------------------------------------------------------

  reloadCardPools(): void {
    this.http
      .get<CardPool[]>(`${this.base}/card-pools`)
      .subscribe({ next: (l) => this.cardPools.set(l) });
  }

  saveCardPool(p: CardPool, originalName?: string): Observable<CardPool> {
    return this.upsert<CardPool>("card-pools", p.name, originalName, {
      description: p.description,
      cards: p.cards,
    }).pipe(tap(() => this.reloadCardPools()));
  }

  removeCardPool(name: string): Observable<void> {
    return this.del("card-pools", name).pipe(tap(() => this.reloadCardPools()));
  }

  // -- BIN ranges ---------------------------------------------------------

  reloadBinRanges(): void {
    this.http
      .get<BinRange[]>(`${this.base}/bin-ranges`)
      .subscribe({ next: (l) => this.binRanges.set(l) });
  }

  saveBinRange(b: BinRange, originalName?: string): Observable<BinRange> {
    return this.upsert<BinRange>("bin-ranges", b.name, originalName, {
      description: b.description,
      bin: b.bin,
      length: b.length,
      brand: b.brand,
    }).pipe(tap(() => this.reloadBinRanges()));
  }

  removeBinRange(name: string): Observable<void> {
    return this.del("bin-ranges", name).pipe(tap(() => this.reloadBinRanges()));
  }

  /** Generate `count` Luhn-valid PANs; optionally persist them as a card pool. */
  generatePans(
    name: string,
    count: number,
    asPool?: string,
  ): Observable<{ pans: string[]; saved_pool: string | null }> {
    let url = `${this.base}/bin-ranges/${encodeURIComponent(name)}/generate?count=${count}`;
    if (asPool) url += `&as_pool=${encodeURIComponent(asPool)}`;
    return this.http
      .post<{ pans: string[]; saved_pool: string | null }>(url, {})
      .pipe(tap(() => asPool && this.reloadCardPools()));
  }

  // -- terminal pools -----------------------------------------------------

  reloadTerminalPools(): void {
    this.http
      .get<TerminalPool[]>(`${this.base}/terminal-pools`)
      .subscribe({ next: (l) => this.terminalPools.set(l) });
  }

  saveTerminalPool(
    p: TerminalPool,
    originalName?: string,
  ): Observable<TerminalPool> {
    return this.upsert<TerminalPool>("terminal-pools", p.name, originalName, {
      description: p.description,
      terminals: p.terminals,
    }).pipe(tap(() => this.reloadTerminalPools()));
  }

  removeTerminalPool(name: string): Observable<void> {
    return this.del("terminal-pools", name).pipe(
      tap(() => this.reloadTerminalPools()),
    );
  }

  // -- keys ---------------------------------------------------------------

  reloadKeys(): void {
    this.http
      .get<KeyView[]>(`${this.base}/keys`)
      .subscribe({ next: (l) => this.keys.set(l) });
  }

  /** Save a key. An empty `value` keeps the existing material server-side. */
  saveKey(
    name: string,
    body: { description: string; type: KeyType; value: string },
    originalName?: string,
  ): Observable<KeyView> {
    return this.upsert<KeyView>("keys", name, originalName, body).pipe(
      tap(() => this.reloadKeys()),
    );
  }

  removeKey(name: string): Observable<void> {
    return this.del("keys", name).pipe(tap(() => this.reloadKeys()));
  }

  nameExists(
    list: { name: string }[],
    name: string,
    exclude?: string,
  ): boolean {
    return list.some((x) => x.name === name && x.name !== exclude);
  }

  // -- shared helpers -----------------------------------------------------

  /** Upsert; a rename deletes the old record first, then PUTs the new name. */
  private upsert<T>(
    kind: string,
    name: string,
    originalName: string | undefined,
    body: unknown,
  ): Observable<T> {
    const put = () =>
      this.http.put<T>(
        `${this.base}/${kind}/${encodeURIComponent(name)}`,
        body,
      );
    if (originalName && originalName !== name) {
      return new Observable<T>((sub) => {
        this.del(kind, originalName).subscribe({
          next: () => put().subscribe(sub),
          error: (e) => sub.error(e),
        });
      });
    }
    return put();
  }

  private del(kind: string, name: string): Observable<void> {
    return this.http.delete<void>(
      `${this.base}/${kind}/${encodeURIComponent(name)}`,
    );
  }
}
