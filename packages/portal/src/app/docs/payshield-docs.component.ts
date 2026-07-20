import {
  Component,
  ChangeDetectionStrategy,
  signal,
  computed,
} from "@angular/core";
import { RouterLink } from "@angular/router";

import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { IconComponent } from "../shared/ui/icon.component";
import { DocsNavComponent } from "./docs-nav.component";
import { DocsTocComponent, TocItem } from "./docs-toc.component";

interface Cmd {
  code: string;
  action: string;
  purpose: string;
  params: string;
}
interface CmdGroup {
  title: string;
  cmds: Cmd[];
}

/**
 * Dedicated in-app reference for the payShield 10K HSM. Renovated UX: full-width
 * two-column layout with a sticky on-this-page TOC (scroll-spy), a live filter
 * over the command catalogue, and data-driven command tables. Code blocks are
 * bound as strings so their braces don't collide with Angular interpolation.
 */
@Component({
  selector: "app-payshield-docs",
  standalone: true,
  imports: [
    PageHeaderComponent,
    RouterLink,
    IconComponent,
    DocsNavComponent,
    DocsTocComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="doc">
      <pp-page-header
        subtitle="payShield 10K HSM — configuration & command reference"
      />

      <div class="doc__body">
        <app-docs-nav />

        <div class="doc__grid">
          <div class="doc__main">
            <!-- Overview -->
            <section id="overview" class="grp">
              <h2><span class="k">Explanation</span> The two sides</h2>
              <p>
                An HSM integration has two halves and PayProbe gives you both:
                the <strong>HSM</strong> (server) listens on a port and answers
                host commands holding keys under its LMK; the
                <strong>host</strong> (client) is your application sending
                commands and reading replies. Start the simulator once, then
                scenarios drive it as a client — no physical appliance needed.
              </p>
              <table class="tbl">
                <thead>
                  <tr>
                    <th>Side</th>
                    <th>What it is</th>
                    <th>PayProbe piece</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>HSM (server)</td>
                    <td>
                      Listens, answers host commands, holds keys under the LMK
                    </td>
                    <td>
                      <code>PayShieldSimulator</code> — a saved
                      <a routerLink="/simulators">Simulator</a> with
                      <code>protocol: "payshield"</code>
                    </td>
                  </tr>
                  <tr>
                    <td>Host (client)</td>
                    <td>Sends commands, reads replies</td>
                    <td>
                      <code>HSMAdapter</code> — the
                      <code>payshield</code> adapter, used as a scenario target
                    </td>
                  </tr>
                </tbody>
              </table>
              <p class="note">
                <pp-icon name="shield" [size]="15" />
                The simulator performs <strong>real</strong> payment crypto
                under a <em>test</em> LMK, so values verify across commands. Key
                material is random and tokens only round-trip within the
                simulator — use it for protocol and integration testing, never
                for real key security.
              </p>
            </section>

            <!-- Simulator config -->
            <section id="simulator-config" class="grp">
              <h2><span class="k">Reference</span> Simulator configuration</h2>
              <p class="muted">
                Every field of a <code>protocol: "payshield"</code> config.
              </p>
              <pre><code>{{ simulatorConfig }}</code></pre>
              <ul class="notes">
                <li>
                  <b>header_bytes</b> must match the message-header length on
                  the host port you emulate (payShield uses 4).
                </li>
                <li>
                  <b>lmk</b> only needs to be internally consistent — KCVs and
                  all payment crypto come from the clear key, not this value.
                </li>
                <li>
                  <b>variant_lmk</b> — when true, enforces key separation (a CVK
                  won't work as a ZPK); off by default.
                </li>
                <li>
                  <b>disabled_commands</b> — listed commands return error
                  <code>68</code>.
                </li>
                <li>
                  <b>commands</b> — scripted overrides, e.g.
                  <code>"CY": &#123; "error": "01" &#125;</code> forces every
                  CVV verification to fail.
                </li>
              </ul>
              <p class="muted">
                Start it from the
                <a routerLink="/simulators">Simulators</a> page (payShield 10K
                preset → enable → Save → Start), the CLI, or the MCP tool
                <code>start_payshield_simulator</code>.
              </p>
            </section>

            <!-- Client config -->
            <section id="client-config" class="grp">
              <h2>
                <span class="k">Reference</span> Client / environment
                configuration
              </h2>
              <p class="muted">
                Scenarios reach the HSM through an
                <a routerLink="/environments">Environment</a> that maps the
                <code>hsm</code> target to the <code>payshield</code> adapter.
              </p>
              <pre><code>{{ clientConfig }}</code></pre>
              <p>
                A step targets <code>hsm</code> and names an
                <strong>action</strong>. Replies land in
                <code>response_payload</code>: <code>response_code</code>,
                <code>error_code</code>, <code>ok</code>, <code>data</code>,
                plus an action-specific field (<code>cvv</code>,
                <code>key</code>, <code>kcv</code>, <code>pin_block</code>,
                <code>arpc</code>).
              </p>
            </section>

            <!-- Wire / correlation -->
            <section id="wire" class="grp">
              <h2>
                <span class="k">Explanation</span> Wire format &amp; correlation
              </h2>
              <pre><code>{{ wireFormat }}</code></pre>
              <p>
                The <strong>header</strong> is a host-chosen value the HSM
                echoes back — how the host matches a reply to its request. The
                client tags each command with an
                <strong>incrementing</strong> header, so many commands can be in
                flight on one connection, demultiplexed by header. Set
                <code>connections</code> &gt; 1 for a pool of sockets. The
                <strong>response code</strong> is the command with its 2nd
                character incremented: <code>A0→A1</code>, <code>CW→CX</code>,
                <code>NC→ND</code>.
              </p>
            </section>

            <!-- Commands (searchable) -->
            <section id="commands" class="grp">
              <h2><span class="k">Reference</span> Supported commands</h2>
              <div class="search">
                <pp-icon name="search" [size]="15" />
                <input
                  type="text"
                  [value]="query()"
                  (input)="onSearch($event)"
                  placeholder="Filter commands — code, action or keyword…"
                  aria-label="Filter commands"
                />
                @if (query()) {
                  <button
                    class="clear"
                    (click)="query.set('')"
                    aria-label="Clear"
                  >
                    ✕
                  </button>
                }
              </div>

              @for (g of filteredGroups(); track g.title) {
                <h3>{{ g.title }}</h3>
                <table class="tbl">
                  <thead>
                    <tr>
                      <th>Cmd</th>
                      <th>Action</th>
                      <th>Purpose</th>
                      <th>Key params</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (c of g.cmds; track c.action) {
                      <tr>
                        <td class="mono">{{ c.code }}</td>
                        <td>
                          <code>{{ c.action }}</code>
                        </td>
                        <td>{{ c.purpose }}</td>
                        <td class="muted">{{ c.params }}</td>
                      </tr>
                    }
                  </tbody>
                </table>
              }
              @if (!filteredGroups().length) {
                <p class="muted empty">No commands match “{{ query() }}”.</p>
              }
              <p class="muted">
                An unknown action falls back to a raw passthrough (<code
                  >action: "raw"</code
                >
                with <code>command</code> + <code>data</code>). Unknown commands
                return error <code>30</code>.
              </p>
            </section>

            <!-- Error codes + key types -->
            <section id="reference-tables" class="grp">
              <h2>
                <span class="k">Reference</span> Error codes &amp; key types
              </h2>
              <div class="two-col">
                <div>
                  <h3>Error codes</h3>
                  <table class="tbl">
                    <thead>
                      <tr>
                        <th>Code</th>
                        <th>Meaning</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        <td class="mono">00</td>
                        <td>Success</td>
                      </tr>
                      <tr>
                        <td class="mono">01</td>
                        <td>Verification failed (CVV / PIN / MAC / ARQC)</td>
                      </tr>
                      <tr>
                        <td class="mono">10</td>
                        <td>Key parity error</td>
                      </tr>
                      <tr>
                        <td class="mono">15</td>
                        <td>Invalid input data / parse error</td>
                      </tr>
                      <tr>
                        <td class="mono">30</td>
                        <td>Unknown / unsupported command</td>
                      </tr>
                      <tr>
                        <td class="mono">68</td>
                        <td>Command disabled</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div>
                  <h3>Common key types</h3>
                  <table class="tbl">
                    <thead>
                      <tr>
                        <th>Code</th>
                        <th>Key</th>
                        <th>Used by</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        <td class="mono">000</td>
                        <td>ZMK</td>
                        <td>A6 / K8</td>
                      </tr>
                      <tr>
                        <td class="mono">001</td>
                        <td>ZPK</td>
                        <td>CA / CC / EC / G0</td>
                      </tr>
                      <tr>
                        <td class="mono">002</td>
                        <td>TPK / PVK / TMK</td>
                        <td>CA / EC</td>
                      </tr>
                      <tr>
                        <td class="mono">008</td>
                        <td>ZAK / TAK (MAC)</td>
                        <td>M6 / M8</td>
                      </tr>
                      <tr>
                        <td class="mono">109</td>
                        <td>MK-AC (EMV)</td>
                        <td>KQ</td>
                      </tr>
                      <tr>
                        <td class="mono">302</td>
                        <td>BDK / IPEK</td>
                        <td>G0</td>
                      </tr>
                      <tr>
                        <td class="mono">402</td>
                        <td>CVK</td>
                        <td>CW / CY</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </section>

            <!-- Quick start -->
            <section id="quick-start" class="grp">
              <h2><span class="k">Tutorial</span> Quick start</h2>
              <pre><code>{{ quickStart }}</code></pre>
              <p class="muted">
                For load, point the <code>hsm</code> target at the simulator
                with <code>"connections": 8</code> and drive commands in
                parallel — each socket pipelines and the pool round-robins. See
                the <a routerLink="/simulators">Simulators</a> page and the
                <strong>HSM Tests</strong> project's
                <code>payshield_hsm_full</code> scenario.
              </p>
            </section>
          </div>

          <aside class="doc__aside">
            <app-docs-toc [items]="toc" />
          </aside>
        </div>
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .doc {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .doc__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: 18px 22px;
        display: flex;
        flex-direction: column;
        gap: 16px;
      }
      .doc__grid {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 220px;
        gap: 32px;
      }
      .doc__main {
        display: flex;
        flex-direction: column;
        gap: 26px;
        min-width: 0;
      }
      .doc__aside {
        display: block;
      }
      @media (max-width: 920px) {
        .doc__grid {
          grid-template-columns: 1fr;
        }
        .doc__aside {
          display: none;
        }
      }
      .grp {
        scroll-margin-top: 8px;
      }
      .grp h2 {
        font-size: 16px;
        margin: 0 0 8px;
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .grp h3 {
        font-size: 13px;
        margin: 18px 0 6px;
      }
      .k {
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        font-weight: 700;
        color: var(--color-primary, #58a6ff);
        border: 1px solid var(--color-primary, #58a6ff);
        border-radius: 999px;
        padding: 2px 8px;
      }
      p {
        font-size: 14px;
        line-height: 1.65;
        margin: 6px 0;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
      }
      .empty {
        padding: 10px 0;
      }
      .note {
        font-size: 13px;
        border-left: 3px solid var(--color-primary, #58a6ff);
        padding: 10px 12px;
        background: var(--color-node, transparent);
        border-radius: 0 8px 8px 0;
        display: flex;
        gap: 8px;
        align-items: flex-start;
      }
      a {
        color: var(--color-primary, #58a6ff);
      }
      code {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 0.86em;
        background: var(--color-node, transparent);
        padding: 1px 5px;
        border-radius: 4px;
      }
      .mono {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 12.5px;
      }
      pre {
        background: var(--color-node, var(--color-bg));
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 14px;
        overflow-x: auto;
        margin: 8px 0;
      }
      pre code {
        background: none;
        padding: 0;
        font-size: 12.5px;
        line-height: 1.55;
      }
      ul.notes {
        margin: 8px 0;
        padding-left: 18px;
        line-height: 1.75;
        font-size: 13.5px;
      }
      .tbl {
        width: 100%;
        border-collapse: collapse;
        margin: 8px 0;
        font-size: 13px;
      }
      .tbl th,
      .tbl td {
        border: 1px solid var(--color-border);
        padding: 7px 10px;
        text-align: left;
        vertical-align: top;
      }
      .tbl th {
        background: var(--color-node, transparent);
        font-weight: 600;
        position: sticky;
        top: 0;
      }
      .tbl code {
        background: none;
        padding: 0;
      }
      .two-col {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 20px;
      }
      @media (max-width: 720px) {
        .two-col {
          grid-template-columns: 1fr;
        }
      }
      .search {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 10px 0 4px;
        border: 1px solid var(--color-border);
        border-radius: 9px;
        padding: 8px 12px;
        background: var(--color-node, var(--color-bg));
        max-width: 460px;
      }
      .search:focus-within {
        border-color: var(--color-primary, #58a6ff);
      }
      .search pp-icon {
        color: var(--color-text-muted, var(--color-text));
      }
      .search input {
        flex: 1;
        border: none;
        background: transparent;
        color: var(--color-text);
        font-size: 13.5px;
        outline: none;
      }
      .search .clear {
        border: none;
        background: transparent;
        color: var(--color-text-muted, var(--color-text));
        cursor: pointer;
        font-size: 13px;
        padding: 0 2px;
      }
    `,
  ],
})
export class PayshieldDocsComponent {
  readonly query = signal("");

  readonly toc: TocItem[] = [
    { id: "overview", label: "Overview" },
    { id: "simulator-config", label: "Simulator config" },
    { id: "client-config", label: "Client config" },
    { id: "wire", label: "Wire & correlation" },
    { id: "commands", label: "Commands" },
    { id: "reference-tables", label: "Error codes & key types" },
    { id: "quick-start", label: "Quick start" },
  ];

  readonly commandGroups: CmdGroup[] = [
    {
      title: "Diagnostics & status",
      cmds: [
        {
          code: "NC→ND",
          action: "nc",
          purpose: "Sign-on / health check; returns LMK check value + firmware",
          params: "—",
        },
        {
          code: "NO→NP",
          action: "hsm_status",
          purpose: "HSM status; mode 01 reports PCI-HSM compliance",
          params: "mode",
        },
      ],
    },
    {
      title: "Key management",
      cmds: [
        {
          code: "A0→A1",
          action: "generate_key",
          purpose: "Mint a working key under the LMK; returns token + KCV",
          params: "key_type, scheme",
        },
        {
          code: "BU→BV",
          action: "key_check",
          purpose: "Compute a key's check value (confirm both ends hold it)",
          params: "key_type_code, key",
        },
        {
          code: "A6→A7",
          action: "import_key",
          purpose: "Import a key arriving encrypted under a shared ZMK",
          params: "zmk, key, key_type",
        },
        {
          code: "K8→K9",
          action: "export_key",
          purpose: "Export an LMK key re-encrypted under a shared KEK",
          params: "key, kek, key_type",
        },
      ],
    },
    {
      title: "Card verification (CVV / CVC)",
      cmds: [
        {
          code: "CW→CX",
          action: "generate_cvv",
          purpose: "Generate the card security value (magstripe / CVV2 / iCVV)",
          params: "cvk, pan, expiry, service_code",
        },
        {
          code: "CY→CZ",
          action: "verify_cvv",
          purpose: "Authorization-time check; error 01 on mismatch",
          params: "cvk, cvv, pan, expiry",
        },
      ],
    },
    {
      title: "PIN processing",
      cmds: [
        {
          code: "CA→CB",
          action: "translate_pin",
          purpose: "Translate a PIN block TPK→ZPK (terminal → network)",
          params: "tpk, zpk, pin_block, pan",
        },
        {
          code: "CC→CD",
          action: "translate_pin_zpk_zpk",
          purpose: "Translate a PIN block ZPK→ZPK (zone boundary)",
          params: "src_zpk, dst_zpk, pin_block, pan",
        },
        {
          code: "EC→ED",
          action: "verify_pin",
          purpose: "Issuer PIN verification (Visa PVV method)",
          params: "zpk, pvk, pin_block, pvv, pan",
        },
        {
          code: "G0→G1",
          action: "dukpt_translate_pin",
          purpose: "Translate a DUKPT (BDK) PIN block to a ZPK",
          params: "bdk, zpk, ksn, pin_block, pan",
        },
      ],
    },
    {
      title: "Message authentication & EMV",
      cmds: [
        {
          code: "M6→M7",
          action: "generate_mac",
          purpose: "Retail MAC (ISO 9797-1 alg. 3) over a data block",
          params: "key, message",
        },
        {
          code: "M8→M9",
          action: "verify_mac",
          purpose: "Verify a MAC; error 01 on tamper/mismatch",
          params: "key, message, mac",
        },
        {
          code: "KQ→KR",
          action: "verify_arqc",
          purpose: "Validate an EMV ARQC, optionally generate the ARPC",
          params: "mk_ac, pan, atc, txn_data, arqc",
        },
      ],
    },
  ];

  readonly filteredGroups = computed<CmdGroup[]>(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return this.commandGroups;
    return this.commandGroups
      .map((g) => ({
        title: g.title,
        cmds: g.cmds.filter((c) =>
          `${c.code} ${c.action} ${c.purpose} ${c.params}`
            .toLowerCase()
            .includes(q),
        ),
      }))
      .filter((g) => g.cmds.length > 0);
  });

  onSearch(ev: Event): void {
    this.query.set((ev.target as HTMLInputElement).value);
  }

  readonly simulatorConfig = `{
  "protocol": "payshield",        // selects the HSM simulator (required)
  "host": "0.0.0.0",              // bind address
  "port": 1500,                   // classic payShield host port
  "header_bytes": 4,              // width of the echoed message header
  "framing": {
    "length_prefix_bytes": 2,     // 2-byte big-endian length prefix
    "length_byte_order": "big",
    "encoding": "ascii"
  },
  "lmk": "0123456789ABCDEFFEDCBA9876543210",   // double-length test LMK
  "firmware": "0007-E000",        // string the NC command reports
  "variant_lmk": false,           // true = enforce key separation
  "pci_hsm": false,               // flag the NO command reports
  "disabled_commands": [],        // e.g. ["CW"] -> error 68
  "commands": {}                  // scripted per-command overrides
}`;

  readonly clientConfig = `{
  "name": "payshield-sim",
  "adapters": {
    "hsm": {
      "adapter": "payshield",     // the HSMAdapter client
      "host": "127.0.0.1",
      "port": 1500,
      "header_bytes": 4,
      "connections": 1,           // sockets to open; >1 for load
      "length_prefix_bytes": 2,
      "length_byte_order": "big",
      "encoding": "ascii",
      "response_timeout_sec": 5
    }
  }
}`;

  readonly wireFormat = `[2-byte length][header / correlation id][2-char command][command data]
        framing              incrementing id            e.g. A0`;

  readonly quickStart = `# 1. Start the HSM simulator (persisted; shows on the Simulators page)
start_payshield_simulator(label="payShield 10K", port=1500)

# 2. Probe it
hsm_command(action="nc")            # -> ND 00, firmware 0007-E000

# 3. Generate a CVK, make a CVV, verify it
g   = hsm_command(action="generate_key", params={"key_type":"402","scheme":"U"})
cvk = g["response"]["key"]
cv  = hsm_command(action="generate_cvv",
                  params={"cvk":cvk,"pan":"4000000000000002",
                          "expiry":"2512","service_code":"000"})
hsm_command(action="verify_cvv",
            params={"cvk":cvk,"cvv":cv["response"]["cvv"],
                    "pan":"4000000000000002","expiry":"2512","service_code":"000"})
# -> error_code "00"  (verified)`;
}
