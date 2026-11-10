import {
  Connection,
  blankConnection,
  extraKeyNames,
  fromAdapterConfig,
  healthCheckToApi,
  toAdapterConfig,
} from "./connection.models";

/** The form's round-trip of a stored connection must never drop a key it has
 *  no field for: a portal save on 2026-11-09 lost `health_check` and
 *  `message_format_id`, which turned a healthy host into "unhealthy" and
 *  decoded approvals as DE 39 "94". */
describe("connection.models round-trip", () => {
  const posHost = {
    name: "pos_host",
    adapter: "tcp",
    protocol: "iso8583",
    host: "10.0.1.50",
    port: 7001,
    mode: "outbound",
    disabled: false,
    default: false,
    listen_port: null,
    description: "POS host (test)",
    environment_overrides: {},
    external: false,
    response_timeout_sec: 15,
    health_check: { mode: "connect" },
    message_format_id: "pos-dialect",
    framing: {
      length_prefix_bytes: 4,
      length_encoding: "ascii",
      header: "ISO",
    },
    sign_on: {
      enabled: true,
      payload: { mti: "0800", values: { "70": "001" } },
    },
    mac: { key_ref: "zak-1", fields: "64" },
    mhdr: "6000000000",
  };

  it("keeps health_check and message_format_id through load → save", () => {
    const form = fromAdapterConfig("pos_host", posHost);
    expect(form.healthCheck.mode).toBe("connect");
    expect(form.messageFormatId).toBe("pos-dialect");
    expect(form.description).toBe("POS host (test)");
    const out = toAdapterConfig(form);
    expect(out["health_check"]).toEqual({ mode: "connect" });
    expect(out["message_format_id"]).toBe("pos-dialect");
    expect(out["response_timeout_sec"]).toBe(15);
  });

  it("re-emits top-level keys the form has no field for", () => {
    const out = toAdapterConfig(fromAdapterConfig("pos_host", posHost));
    expect(out["mac"]).toEqual({ key_ref: "zak-1", fields: "64" });
    expect(out["mhdr"]).toBe("6000000000");
  });

  it("re-emits unmodelled sub-keys of modelled sections under the form's keys", () => {
    const form = fromAdapterConfig("pos_host", posHost);
    const out = toAdapterConfig(form) as Record<string, any>;
    expect(out["framing"]).toEqual({
      length_prefix_bytes: 4,
      length_encoding: "ascii",
      header: "ISO",
    });
    expect(out["sign_on"]).toEqual({
      enabled: true,
      payload: { mti: "0800", values: { "70": "001" } },
    });
    expect(extraKeyNames(form.extra)).toEqual([
      "framing.header",
      "mac",
      "mhdr",
      "sign_on.payload",
    ]);
  });

  it("never carries backend metadata or nulls into extra", () => {
    const form = fromAdapterConfig("pos_host", posHost);
    for (const k of [
      "name",
      "listen_port",
      "mode",
      "disabled",
      "default",
      "environment_overrides",
      "description",
      "external",
    ])
      expect(k in form.extra)
        .withContext(k)
        .toBeFalse();
  });

  it("keeps external: true on a provider connection (ADR-0009 load refusal)", () => {
    const stripe = {
      adapter: "http",
      base_url: "https://api.stripe.com",
      external: true,
      auth: { kind: "bearer", secret_ref: "stripe" },
    };
    const out = toAdapterConfig(fromAdapterConfig("stripe", stripe));
    expect(out["external"]).toBeTrue();
    expect(out["auth"]).toEqual({ kind: "bearer", secret_ref: "stripe" });
    expect(out["base_url"]).toBe("https://api.stripe.com");
  });

  it("does not resurrect a modelled field the user reset to its default", () => {
    const form = fromAdapterConfig("pos_host", posHost);
    form.responseTimeoutSec = 30;
    form.healthCheck.mode = "probe";
    form.messageFormatId = "";
    form.signOn = false;
    const out = toAdapterConfig(form) as Record<string, any>;
    expect("response_timeout_sec" in out).toBeFalse();
    expect("health_check" in out).toBeFalse();
    expect("message_format_id" in out).toBeFalse();
    // the sign-on payload survives, but without `enabled` the worker skips it
    expect(out["sign_on"]).toEqual({
      payload: { mti: "0800", values: { "70": "001" } },
    });
  });

  it("emits the health-check section sparsely and parses its payload", () => {
    expect(healthCheckToApi(blankConnection().healthCheck)).toEqual({});
    expect(
      healthCheckToApi({
        mode: "message",
        action: "",
        payloadJson: '{"mti": "0800", "values": {"70": "001"}}',
        settleMs: null,
      }),
    ).toEqual({
      mode: "message",
      payload: { mti: "0800", values: { "70": "001" } },
    });
    expect(
      healthCheckToApi({
        mode: "connect",
        action: "",
        payloadJson: "",
        settleMs: 500,
      }),
    ).toEqual({ mode: "connect", settle_ms: 500 });
  });

  it("passes unknown keys through for every adapter kind", () => {
    const cases: Record<string, Record<string, unknown>>[] = [
      [
        {
          adapter: "grpc",
          target: "h:1",
          tls: { enabled: true, sni: "x" },
          deadline: 3,
        },
      ],
      [
        {
          adapter: "nats",
          servers: ["nats://b:4222"],
          auth: { token: "t", user: "u" },
          tls: {},
        },
      ],
      [{ adapter: "payshield", host: "h", port: 1500, lmk_id: "01" }],
      [
        {
          adapter: "db_probe_core",
          host: "h",
          port: 5432,
          engine: "postgresql",
          ssl: "require",
        },
      ],
    ].map((c) => ({ cfg: c[0] }));
    for (const { cfg } of cases) {
      const out = toAdapterConfig(fromAdapterConfig("x", cfg)) as Record<
        string,
        any
      >;
      for (const k of Object.keys(cfg)) {
        if (k === "tls" && cfg["adapter"] === "nats") continue; // empty object: nothing to keep
        expect(out[k]).withContext(`${cfg["adapter"]}.${k}`).toEqual(cfg[k]);
      }
    }
  });

  it("a blank connection has an empty passthrough", () => {
    const b: Connection = blankConnection("n");
    expect(b.extra).toEqual({});
    expect("mac" in toAdapterConfig(b)).toBeFalse();
  });
});
