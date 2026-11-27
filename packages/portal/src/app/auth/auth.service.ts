import { HttpClient } from "@angular/common/http";
import { Injectable, computed, inject, signal } from "@angular/core";
import { Observable, catchError, map, of, tap } from "rxjs";

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

/** Credentials a public demo publishes for its login page (auth-service
 *  `AUTH_DEMO_LOGIN`). */
export interface DemoLogin {
  username: string;
  password: string;
}

const TOKEN_KEY = "pp.auth.token";
const USER_KEY = "pp.auth.user";
/** Set while the session came from an authenticating proxy (`/proxy-login`). */
const PROXY_KEY = "pp.auth.proxy";

/**
 * Holds the session: the JWT issued by auth-service and the decoded user.
 *
 * The token is persisted in localStorage so a reload keeps you signed in; the
 * interceptor reads `token()` to attach the bearer header, and the guard reads
 * `isAuthenticated()`. Call `login()` from the login page and `logout()` from
 * the topbar menu.
 *
 * Behind an authenticating proxy (auth-service `AUTH_PROXY_HEADER`), the login
 * page calls `proxyLogin()` first and signs straight in. Such a session also
 * signs out of the proxy on `logout()`; otherwise the login page would sign
 * the person straight back in.
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
      .pipe(tap((res) => this.startSession(res, false)));
  }

  /** Sign in as the person an authenticating proxy already signed in. Emits
   *  `true` with the session stored, or `false` when proxy sign-in is off
   *  (404), the proxy sent no usable identity (401) or auth-service is
   *  unreachable. Never throws: the login form is shown either way. */
  proxyLogin(): Observable<boolean> {
    return this.http.get<TokenResponse>(`${this.base}/proxy-login`).pipe(
      tap((res) => this.startSession(res, true)),
      map(() => true),
      catchError(() => of(false)),
    );
  }

  /** True while the session came from an authenticating proxy. */
  get viaProxy(): boolean {
    return localStorage.getItem(PROXY_KEY) === "1";
  }

  /** The demo account to pre-fill, or `null` when demo login is off (404) or
   *  auth-service is unreachable. Never throws: the login page works either way. */
  demoLogin(): Observable<DemoLogin | null> {
    return this.http
      .get<DemoLogin>(`${this.base}/demo-login`)
      .pipe(catchError(() => of(null)));
  }

  /** Verify the current token and load the user's roles / project scope. */
  refreshUser(): Observable<UserInfo> {
    return this.http
      .get<UserInfo>(`${this.base}/me`)
      .pipe(tap((u) => this.setUser(u)));
  }

  /** Sign out. A session that came from an authenticating proxy also leaves
   *  for the proxy's logout path, which ends the proxy's own session. */
  logout(): void {
    const viaProxy = this.viaProxy;
    this.clearSession();
    if (viaProxy) this.redirect(this.cfg.proxyLogoutPath);
  }

  /** Forget the token and user in this browser only (an expired token): a
   *  proxy session is left alone, so the login page can sign straight in. */
  clearSession(): void {
    this._token.set(null);
    this._user.set(null);
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    localStorage.removeItem(PROXY_KEY);
  }

  private startSession(res: TokenResponse, viaProxy: boolean): void {
    this.setToken(res.access_token);
    if (viaProxy) localStorage.setItem(PROXY_KEY, "1");
    else localStorage.removeItem(PROXY_KEY);
    this.refreshUser().subscribe({ error: () => {} });
  }

  /** Full-page navigation (the proxy's logout is not a portal route). */
  private redirect(url: string): void {
    window.location.assign(url);
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
