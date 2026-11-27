import { provideHttpClient } from "@angular/common/http";
import {
  HttpTestingController,
  provideHttpClientTesting,
} from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import { DEFAULT_ADVERTISED_HOST, SystemService } from "./system.service";

describe("SystemService advertised listener host", () => {
  let system: SystemService;
  let http: HttpTestingController;
  let url: string;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    system = TestBed.inject(SystemService);
    http = TestBed.inject(HttpTestingController);
    url = `${TestBed.inject(RuntimeConfigService).runApiBase}/system`;
  });

  afterEach(() => http.verify());

  function advertised(answer: (url: string) => void): string | undefined {
    let host: string | undefined;
    system.advertisedHost().subscribe((h) => (host = h));
    answer(url);
    return host;
  }

  it("is the host the orchestrator advertises", () => {
    const host = advertised((u) =>
      http.expectOne(u).flush({
        network: {
          listen_host: "0.0.0.0",
          advertised_host: "payprobe.example.test",
        },
      }),
    );
    expect(host).toBe("payprobe.example.test");
  });

  it("falls back to loopback when the orchestrator does not say", () => {
    expect(DEFAULT_ADVERTISED_HOST).toBe("127.0.0.1");
    expect(advertised((u) => http.expectOne(u).flush({}))).toBe(
      DEFAULT_ADVERTISED_HOST,
    );
  });

  it("falls back to loopback when the orchestrator is unreachable", () => {
    expect(
      advertised((u) =>
        http.expectOne(u).flush(null, { status: 502, statusText: "down" }),
      ),
    ).toBe(DEFAULT_ADVERTISED_HOST);
  });
});
