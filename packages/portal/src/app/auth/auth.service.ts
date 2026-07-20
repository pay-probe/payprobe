import { HttpClient } from "@angular/common/http";
import { Injectable, computed, inject, signal } from "@angular/core";
import { Observable, tap } from "rxjs";

import { RuntimeConfigService } from "../shared/runtime-config.service";

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface UserInfo {
  username: string;
  roles: string[];
  project_ids: string[];
}

const TOKEN_KEY = "pp.auth.token";
const USER_KEY = "pp.auth.user";

/**
 * Holds the session: the JWT issued by auth-service and the decoded user.
 *
 * The token is persisted in localStorage so a reload keeps you signed in; the
 * interceptor reads `token()` to attach the bearer header, and the guard reads
 * `isAuthenticated()`. Call `login()` from the login page and `logout()` from
 * the topbar menu.
 */
@Injectable({ providedIn: "root" })
export class AuthService {
  private readonly http = inject(HttpClient);
  private readonly cfg = inject(RuntimeConfigService);
  /** Resolved per call so Settings → Endpoints overrides take effect live. */
  private get base(): string {
    return this.cfg.authApiBase;
  }

  private readonly _token = signal<string | null>(
    localStorage.getItem(TOKEN_KEY),
  );
  private readonly _user = signal<UserInfo | null>(this.readStoredUser());

  readonly token = this._token.asReadonly();
  readonly user = this._user.asReadonly();
  readonly isAuthenticated = computed(() => this._token() !== null);

  /** OAuth2 password grant. Persists the token + caches the user on success. */
  login(username: string, password: string): Observable<TokenResponse> {
    const body = new URLSearchParams();
    body.set("username", username);
    body.set("password", password);
    return this.http
      .post<TokenResponse>(`${this.base}/token`, body.toString(), {
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
      })
      .pipe(
        tap((res) => {
          this.setToken(res.access_token);
          this.refreshUser().subscribe({ error: () => {} });
        }),
      );
  }

  /** Verify the current token and load the user's roles / project scope. */
  refreshUser(): Observable<UserInfo> {
    return this.http
      .get<UserInfo>(`${this.base}/me`)
      .pipe(tap((u) => this.setUser(u)));
  }

  logout(): void {
    this._token.set(null);
    this._user.set(null);
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  }

  private setToken(token: string): void {
    this._token.set(token);
    localStorage.setItem(TOKEN_KEY, token);
  }

  private setUser(user: UserInfo): void {
    this._user.set(user);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  }

  private readStoredUser(): UserInfo | null {
    const raw = localStorage.getItem(USER_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as UserInfo;
    } catch {
      return null;
    }
  }
}
