import { Injectable, computed, inject, signal } from "@angular/core";

import { EnvironmentsService } from "../environments/environments.service";

const STORAGE_KEY = "payprobe.activeEnv";

/**
 * Shared "current environment" context for the whole platform.
 *
 * Two layers, so the environment can be chosen widely instead of re-picked on
 * every page:
 *  - **platform default** — persisted (localStorage), shared across sessions and
 *    pages. Set via the topbar's "set as default".
 *  - **session override** — in-memory only; lets the current session work against
 *    a different environment without changing the saved default. Cleared on
 *    reload.
 *
 * `active` resolves the session override first, else the platform default. Pages
 * (Load Test, Run, …) read `active` as their default environment and may still
 * override per-action.
 */
@Injectable({ providedIn: "root" })
export class ActiveEnvironmentService {
  private readonly envs = inject(EnvironmentsService);

  /** Persisted platform-wide default environment name (null = none chosen). */
  readonly defaultEnv = signal<string | null>(this.loadDefault());
  /** Per-session override (null = follow the platform default). */
  readonly sessionEnv = signal<string | null>(null);

  /** The environment in effect now: session override wins, else the default. */
  readonly active = computed(() => this.sessionEnv() ?? this.defaultEnv());
  /** True when the session is temporarily diverging from the saved default. */
  readonly overridden = computed(() => {
    const s = this.sessionEnv();
    return s !== null && s !== this.defaultEnv();
  });

  /** Available environments (list owned by EnvironmentsService). */
  readonly options = this.envs.environments;

  constructor() {
    if (!this.envs.environments().length) this.envs.reload();
  }

  /** Choose the environment for this session only (does not touch the default). */
  select(name: string | null): void {
    this.sessionEnv.set(name);
  }

  /** Persist a new platform-wide default and align the session to it. */
  setDefault(name: string | null): void {
    this.defaultEnv.set(name);
    try {
      if (name) localStorage.setItem(STORAGE_KEY, name);
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* storage unavailable — keep the in-memory value */
    }
    this.sessionEnv.set(null); // the new default is now what's active
  }

  /** Save whatever's active right now as the platform default. */
  pinActiveAsDefault(): void {
    this.setDefault(this.active());
  }

  /** Drop the session override and fall back to the platform default. */
  clearOverride(): void {
    this.sessionEnv.set(null);
  }

  private loadDefault(): string | null {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  }
}
