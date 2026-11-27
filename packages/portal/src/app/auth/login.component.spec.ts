import { TestBed } from "@angular/core/testing";
import {
  ActivatedRoute,
  Router,
  convertToParamMap,
  provideRouter,
} from "@angular/router";
import { Observable, Subject, of } from "rxjs";

import { AuthService } from "./auth.service";
import { LoginComponent } from "./login.component";

describe("LoginComponent sign-in by a trusted proxy", () => {
  let auth: jasmine.SpyObj<AuthService>;
  let navigate: jasmine.Spy;

  async function render(proxy: Observable<boolean>): Promise<HTMLElement> {
    auth = jasmine.createSpyObj<AuthService>("AuthService", [
      "proxyLogin",
      "demoLogin",
      "login",
    ]);
    auth.proxyLogin.and.returnValue(proxy);
    auth.demoLogin.and.returnValue(of(null));
    TestBed.configureTestingModule({
      imports: [LoginComponent],
      providers: [
        provideRouter([]),
        { provide: AuthService, useValue: auth },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: {
              queryParamMap: convertToParamMap({ returnUrl: "/runs" }),
            },
          },
        },
      ],
    });
    navigate = spyOn(TestBed.inject(Router), "navigateByUrl").and.resolveTo(
      true,
    );
    const fixture = TestBed.createComponent(LoginComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    return fixture.nativeElement as HTMLElement;
  }

  it("signs straight in and goes where the sign-in was asked for", async () => {
    const el = await render(of(true));
    expect(navigate).toHaveBeenCalledOnceWith("/runs");
    expect(el.querySelector("form")).toBeNull();
    expect(auth.demoLogin).not.toHaveBeenCalled();
  });

  it("shows the form, as today, when the proxy does not sign in", async () => {
    const el = await render(of(false));
    expect(navigate).not.toHaveBeenCalled();
    expect(el.querySelector("form")).not.toBeNull();
    expect(auth.demoLogin).toHaveBeenCalled();
  });

  it("holds the form back while the proxy is asked", async () => {
    const el = await render(new Subject<boolean>());
    expect(el.querySelector("form")).toBeNull();
    expect(el.querySelector('[role="status"]')?.textContent).toContain(
      "Signing in",
    );
  });
});
