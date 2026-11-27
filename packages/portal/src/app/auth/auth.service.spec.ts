import { provideHttpClient } from "@angular/common/http";
import {
  HttpTestingController,
  provideHttpClientTesting,
} from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import { AuthService, TokenResponse } from "./auth.service";

const SESSION_KEYS = ["pp.auth.token", "pp.auth.user", "pp.auth.proxy"];
const TOKEN: TokenResponse = {
  access_token: "jwt-abc",
  token_type: "bearer",
  expires_in: 3600,
};
const USER = { username: "erin", roles: ["operator"], project_ids: [] };

describe("AuthService sign-in by a trusted proxy", () => {
  let auth: AuthService;
  let http: HttpTestingController;
  let base: string;
  let redirect: jasmine.Spy;

  beforeEach(() => {
    SESSION_KEYS.forEach((k) => localStorage.removeItem(k));
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    auth = TestBed.inject(AuthService);
    http = TestBed.inject(HttpTestingController);
    base = TestBed.inject(RuntimeConfigService).authApiBase;
    // The proxy's logout is a full-page navigation: never leave the test page.
    redirect = spyOn<any>(auth, "redirect");
  });

  afterEach(() => {
    http.verify();
    SESSION_KEYS.forEach((k) => localStorage.removeItem(k));
  });

  function proxySignIn(): boolean | undefined {
    let signedIn: boolean | undefined;
    auth.proxyLogin().subscribe((v) => (signedIn = v));
    http.expectOne(`${base}/proxy-login`).flush(TOKEN);
    http.expectOne(`${base}/me`).flush(USER);
    return signedIn;
  }

  it("stores the token and loads the user, as a password login does", () => {
    expect(proxySignIn()).toBeTrue();
    expect(auth.token()).toBe("jwt-abc");
    expect(localStorage.getItem("pp.auth.token")).toBe("jwt-abc");
    expect(auth.user()).toEqual(USER);
    expect(auth.viaProxy).toBeTrue();
  });

  for (const [status, why] of [
    [404, "proxy sign-in is off"],
    [401, "the proxy sent no usable identity"],
    [0, "auth-service is unreachable"],
  ] as const) {
    it(`answers false with no session when ${why}`, () => {
      let signedIn: boolean | undefined;
      auth.proxyLogin().subscribe((v) => (signedIn = v));
      const req = http.expectOne(`${base}/proxy-login`);
      if (status === 0) req.error(new ProgressEvent("error"));
      else req.flush({ detail: why }, { status, statusText: why });
      expect(signedIn).toBeFalse();
      expect(auth.isAuthenticated()).toBeFalse();
      expect(auth.viaProxy).toBeFalse();
    });
  }

  it("signs a proxy session out of the proxy too", () => {
    proxySignIn();
    auth.logout();
    expect(auth.isAuthenticated()).toBeFalse();
    expect(auth.viaProxy).toBeFalse();
    expect(redirect).toHaveBeenCalledOnceWith(
      TestBed.inject(RuntimeConfigService).proxyLogoutPath,
    );
  });

  it("defaults the proxy logout path to /logout", () => {
    expect(TestBed.inject(RuntimeConfigService).proxyLogoutPath).toBe(
      "/logout",
    );
  });

  it("keeps a password session on the portal at logout", () => {
    proxySignIn();
    auth.login("admin", "admin").subscribe();
    http.expectOne(`${base}/token`).flush(TOKEN);
    http.expectOne(`${base}/me`).flush(USER);
    expect(auth.viaProxy).toBeFalse();
    auth.logout();
    expect(auth.isAuthenticated()).toBeFalse();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("leaves the proxy alone when only the token is cleared", () => {
    proxySignIn();
    auth.clearSession();
    expect(auth.isAuthenticated()).toBeFalse();
    expect(redirect).not.toHaveBeenCalled();
  });
});
