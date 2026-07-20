import {
  Component,
  inject,
  signal,
  computed,
  ChangeDetectionStrategy,
} from "@angular/core";
import { RouterLink } from "@angular/router";

import { RuntimeConfigService } from "../shared/runtime-config.service";
import { PageHeaderComponent } from "../shared/ui/page-header.component";
import { IconComponent } from "../shared/ui/icon.component";
import { DocsNavComponent } from "./docs-nav.component";
import { DocsTocComponent, TocItem } from "./docs-toc.component";

interface DocCard {
  title: string;
  desc: string;
  icon: string;
  route?: string;
  href?: string;
}

/**
 * Docs landing page — organized by the Diátaxis framework (tutorial / how-to /
 * reference / explanation). The "Reference" cards open the live interactive API
 * reference (Scalar) served by each FastAPI service; how-to cards deep-link into
 * the app's interactive tools so the docs are a launchpad, not a dead end.
 */
@Component({
  selector: "app-docs",
  standalone: true,
  imports: [
    PageHeaderComponent,
    RouterLink,
    IconComponent,
    DocsNavComponent,
    DocsTocComponent,
  ],
  template: `
    <div class="docs">
      <pp-page-header>
        <div subtitle>
          <span class="muted"
            >Learn PayProbe — structured by task, with live, interactive
            references</span
          >
        </div>
      </pp-page-header>

      <div class="docs__body">
        <app-docs-nav />

        <div class="doc__grid">
          <div class="doc__main">
            <!-- Hero -->
            <section class="hero">
              <h1>PayProbe documentation</h1>
              <p>
                The end-to-end payment test platform — author scenarios,
                orchestrate three-phase runs, stand up simulators (including a
                payShield HSM), and drive load. Pick a task below, or jump
                straight to a <a routerLink="/docs/payshield">reference</a>.
              </p>
            </section>

            <!-- Platform overview -->
            <section id="platform" class="grp">
              <h2>
                <span class="k">Explanation</span> Platform at a glance
                <button
                  class="toggle"
                  (click)="diagramOpen.set(!diagramOpen())"
                >
                  {{ diagramOpen() ? "Hide diagram" : "Show diagram" }}
                </button>
              </h2>
              <p class="muted">
                How a scenario travels from the editor to a target system — and
                how the load-test fleet plugs into the same engine.
              </p>
              @if (diagramOpen()) {
                <div class="diagram">
                  <svg
                    viewBox="0 0 980 720"
                    role="img"
                    aria-labelledby="paTtl paDesc"
                    xmlns="http://www.w3.org/2000/svg"
                    preserveAspectRatio="xMidYMid meet"
                  >
                    <title id="paTtl">PayProbe platform architecture</title>
                    <desc id="paDesc">
                      Layered diagram: the Angular portal calls backend services
                      through Nginx; services persist to PostgreSQL and stream
                      over Redis; the orchestrator drives the worker engine and
                      the distributed load fleet, which reach target payment
                      systems through the adapter registry.
                    </desc>
                    <defs>
                      <marker
                        id="paAr"
                        viewBox="0 0 10 10"
                        refX="8"
                        refY="5"
                        markerWidth="7"
                        markerHeight="7"
                        orient="auto-start-reverse"
                      >
                        <path d="M0 0L10 5L0 10z" class="pa-arh" />
                      </marker>
                      <marker
                        id="paAra"
                        viewBox="0 0 10 10"
                        refX="8"
                        refY="5"
                        markerWidth="7"
                        markerHeight="7"
                        orient="auto-start-reverse"
                      >
                        <path d="M0 0L10 5L0 10z" class="pa-arha" />
                      </marker>
                    </defs>

                    <text class="pa-ttl" x="40" y="30">
                      PayProbe — Platform Architecture
                    </text>
                    <text class="pa-s" x="40" y="46">
                      End-to-end payment test platform · author → orchestrate →
                      execute → load-test
                    </text>

                    <text class="pa-h" x="40" y="66">CLIENT</text>
                    <rect
                      class="pa-box pa-key"
                      x="40"
                      y="72"
                      width="900"
                      height="52"
                      rx="8"
                    />
                    <text class="pa-t" x="60" y="96">Angular Portal</text>
                    <text class="pa-s" x="60" y="112">
                      Scenario editor · Load Test · Run monitor · Simulators ·
                      Trends · Inspector · Docs
                    </text>

                    <line
                      class="pa-edge"
                      x1="490"
                      y1="124"
                      x2="490"
                      y2="150"
                      marker-end="url(#paAr)"
                    />
                    <text class="pa-el" x="498" y="140">REST + WebSocket</text>

                    <rect
                      class="pa-box"
                      x="40"
                      y="150"
                      width="900"
                      height="38"
                      rx="8"
                    />
                    <text class="pa-t" x="60" y="174">
                      Nginx
                      <tspan class="pa-s">reverse proxy · TLS · routing</tspan>
                    </text>

                    <line class="pa-edge" x1="490" y1="188" x2="490" y2="200" />
                    <line class="pa-edge" x1="145" y1="200" x2="835" y2="200" />
                    <line
                      class="pa-edge"
                      x1="145"
                      y1="200"
                      x2="145"
                      y2="214"
                      marker-end="url(#paAr)"
                    />
                    <line
                      class="pa-edge"
                      x1="375"
                      y1="200"
                      x2="375"
                      y2="214"
                      marker-end="url(#paAr)"
                    />
                    <line
                      class="pa-edge"
                      x1="605"
                      y1="200"
                      x2="605"
                      y2="214"
                      marker-end="url(#paAr)"
                    />
                    <line
                      class="pa-edge"
                      x1="835"
                      y1="200"
                      x2="835"
                      y2="214"
                      marker-end="url(#paAr)"
                    />

                    <text class="pa-h" x="40" y="210">SERVICES (FastAPI)</text>
                    <rect
                      class="pa-box"
                      x="40"
                      y="214"
                      width="210"
                      height="58"
                      rx="8"
                    />
                    <text class="pa-t" x="60" y="238">auth-service</text>
                    <text class="pa-s" x="60" y="254">login · tokens</text>
                    <rect
                      class="pa-box"
                      x="270"
                      y="214"
                      width="210"
                      height="58"
                      rx="8"
                    />
                    <text class="pa-t" x="290" y="238">scenario-service</text>
                    <text class="pa-s" x="290" y="254">
                      scenarios · catalog · flows :8000
                    </text>
                    <rect
                      class="pa-box pa-key"
                      x="500"
                      y="214"
                      width="210"
                      height="58"
                      rx="8"
                    />
                    <text class="pa-t" x="520" y="238">orchestrator</text>
                    <text class="pa-s" x="520" y="254">
                      runs · streams · load coord :8100
                    </text>
                    <rect
                      class="pa-box"
                      x="730"
                      y="214"
                      width="210"
                      height="58"
                      rx="8"
                    />
                    <text class="pa-t" x="750" y="238">report-service</text>
                    <text class="pa-s" x="750" y="254">
                      reports · certification
                    </text>

                    <line
                      class="pa-edge"
                      x1="375"
                      y1="272"
                      x2="300"
                      y2="292"
                      marker-end="url(#paAr)"
                    />
                    <text class="pa-el" x="150" y="288">persist</text>
                    <line
                      class="pa-acc"
                      x1="605"
                      y1="272"
                      x2="690"
                      y2="292"
                      marker-end="url(#paAra)"
                    />
                    <text class="pa-ela" x="612" y="288">
                      events + load bus
                    </text>

                    <text class="pa-h" x="40" y="300">STATE</text>
                    <rect
                      class="pa-box"
                      x="40"
                      y="304"
                      width="430"
                      height="48"
                      rx="8"
                    />
                    <text class="pa-t" x="60" y="326">PostgreSQL</text>
                    <text class="pa-s" x="60" y="342">
                      scenarios · runs · connections · formats · packs
                    </text>
                    <rect
                      class="pa-box pa-key"
                      x="510"
                      y="304"
                      width="430"
                      height="48"
                      rx="8"
                    />
                    <text class="pa-t" x="530" y="326">Redis Streams</text>
                    <text class="pa-s" x="530" y="342">
                      event backbone (resume) + load bus (shards/metrics)
                    </text>

                    <line
                      class="pa-edge"
                      x1="605"
                      y1="272"
                      x2="260"
                      y2="372"
                      marker-end="url(#paAr)"
                    />
                    <text class="pa-el" x="300" y="368">run jobs</text>
                    <line
                      class="pa-acc"
                      x1="725"
                      y1="352"
                      x2="725"
                      y2="372"
                      marker-end="url(#paAra)"
                    />
                    <text class="pa-ela" x="733" y="366">shards · samples</text>

                    <text class="pa-h" x="40" y="380">
                      EXECUTION (asyncio + uvloop)
                    </text>
                    <rect
                      class="pa-box"
                      x="40"
                      y="384"
                      width="430"
                      height="74"
                      rx="8"
                    />
                    <text class="pa-t" x="60" y="408">Worker Engine</text>
                    <text class="pa-s" x="60" y="424">
                      3-phase runner · AdapterRegistry
                    </text>
                    <text class="pa-s" x="60" y="440">
                      concurrency semaphores · 50K / 10K
                    </text>
                    <rect
                      class="pa-box pa-key"
                      x="510"
                      y="384"
                      width="430"
                      height="74"
                      rx="8"
                    />
                    <text class="pa-t" x="530" y="408">Load Fleet</text>
                    <text class="pa-s" x="530" y="424">
                      coordinator → load_worker × N (separate procs)
                    </text>
                    <text class="pa-s" x="530" y="440">
                      steady · ramp · spike · soak · 20K TPS / 100K conns
                    </text>

                    <line
                      class="pa-edge"
                      x1="255"
                      y1="458"
                      x2="470"
                      y2="500"
                      marker-end="url(#paAr)"
                    />
                    <line
                      class="pa-edge"
                      x1="725"
                      y1="458"
                      x2="510"
                      y2="500"
                      marker-end="url(#paAr)"
                    />

                    <text class="pa-h" x="40" y="490">ADAPTER REGISTRY</text>
                    <rect
                      class="pa-box"
                      x="40"
                      y="494"
                      width="900"
                      height="50"
                      rx="8"
                    />
                    <rect
                      class="pa-pill"
                      x="58"
                      y="508"
                      width="118"
                      height="24"
                      rx="12"
                    />
                    <text class="pa-p" x="117" y="524" text-anchor="middle">
                      http
                    </text>
                    <rect
                      class="pa-pill"
                      x="184"
                      y="508"
                      width="118"
                      height="24"
                      rx="12"
                    />
                    <text class="pa-p" x="243" y="524" text-anchor="middle">
                      tcp_iso8583
                    </text>
                    <rect
                      class="pa-pill"
                      x="310"
                      y="508"
                      width="100"
                      height="24"
                      rx="12"
                    />
                    <text class="pa-p" x="360" y="524" text-anchor="middle">
                      hsm
                    </text>
                    <rect
                      class="pa-pill"
                      x="418"
                      y="508"
                      width="100"
                      height="24"
                      rx="12"
                    />
                    <text class="pa-p" x="468" y="524" text-anchor="middle">
                      grpc
                    </text>
                    <rect
                      class="pa-pill"
                      x="526"
                      y="508"
                      width="118"
                      height="24"
                      rx="12"
                    />
                    <text class="pa-p" x="585" y="524" text-anchor="middle">
                      db_probe
                    </text>

                    <line
                      class="pa-edge"
                      x1="490"
                      y1="544"
                      x2="490"
                      y2="572"
                      marker-end="url(#paAr)"
                    />
                    <text class="pa-el" x="498" y="562">
                      real protocol calls
                    </text>

                    <text class="pa-h" x="40" y="582">TARGET SYSTEMS</text>
                    <rect
                      class="pa-box"
                      x="40"
                      y="586"
                      width="900"
                      height="56"
                      rx="8"
                    />
                    <rect
                      class="pa-pill"
                      x="70"
                      y="600"
                      width="150"
                      height="28"
                      rx="8"
                    />
                    <text class="pa-p" x="145" y="618" text-anchor="middle">
                      RestPay
                    </text>
                    <rect
                      class="pa-pill"
                      x="240"
                      y="600"
                      width="170"
                      height="28"
                      rx="8"
                    />
                    <text class="pa-p" x="325" y="618" text-anchor="middle">
                      ISO 8583 Switch
                    </text>
                    <rect
                      class="pa-pill"
                      x="430"
                      y="600"
                      width="120"
                      height="28"
                      rx="8"
                    />
                    <text class="pa-p" x="490" y="618" text-anchor="middle">
                      HSM
                    </text>
                    <rect
                      class="pa-pill"
                      x="570"
                      y="600"
                      width="160"
                      height="28"
                      rx="8"
                    />
                    <text class="pa-p" x="650" y="618" text-anchor="middle">
                      gRPC host
                    </text>

                    <line class="pa-edge" x1="40" y1="688" x2="70" y2="688" />
                    <text class="pa-el" x="78" y="692">
                      control / data flow
                    </text>
                    <line class="pa-acc" x1="232" y1="688" x2="262" y2="688" />
                    <text class="pa-ela" x="270" y="692">
                      Redis load-test path (shards + live metrics)
                    </text>
                  </svg>
                </div>
              }
            </section>

            <!-- Tutorial -->
            <section id="tutorial" class="grp">
              <h2>
                <span class="k">Tutorial</span> Build &amp; run your first
                scenario
              </h2>
              <p class="muted">
                Start here. A guided path from an empty canvas to a green run.
              </p>
              <ol class="steps">
                <li>
                  Open the <a routerLink="/constructor">Scenario editor</a> and
                  drop a <strong>TCP / ISO 8583 → 0200</strong> step from the
                  palette.
                </li>
                <li>
                  Add an assertion on <code>response_code = 00</code> in the
                  step inspector.
                </li>
                <li>
                  (Optional) bind the step to a saved
                  <a routerLink="/connections">Connection</a>.
                </li>
                <li>
                  Hit <strong>Execute</strong>, then watch it live in
                  <a routerLink="/run-monitor">Start run</a>.
                </li>
                <li>
                  Open the <a routerLink="/runs">Run history</a> →
                  <em>Full report</em> for per-step results.
                </li>
              </ol>
            </section>

            <!-- How-to -->
            <section id="howto" class="grp">
              <h2>
                <span class="k">How-to guides</span> Get a specific job done
              </h2>
              <div class="search">
                <pp-icon name="search" [size]="15" />
                <input
                  type="text"
                  [value]="query()"
                  (input)="onSearch($event)"
                  placeholder="Search guides and references…"
                  aria-label="Search docs"
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
              <div class="cards">
                @for (c of howto(); track c.title) {
                  <a class="card" [routerLink]="c.route">
                    <span class="card__ico"
                      ><pp-icon [name]="c.icon" [size]="16"
                    /></span>
                    <b>{{ c.title }}</b>
                    <span>{{ c.desc }}</span>
                  </a>
                }
              </div>
              @if (!howto().length) {
                <p class="muted">No guides match “{{ query() }}”.</p>
              }
            </section>

            <!-- Reference -->
            <section id="reference" class="grp">
              <h2><span class="k">Reference</span> Look something up</h2>
              <div class="cards">
                @for (c of reference(); track c.title) {
                  @if (c.route) {
                    <a class="card" [routerLink]="c.route">
                      <span class="card__ico"
                        ><pp-icon [name]="c.icon" [size]="16"
                      /></span>
                      <b>{{ c.title }}</b>
                      <span>{{ c.desc }}</span>
                    </a>
                  } @else {
                    <a
                      class="card card--api"
                      [attr.href]="c.href"
                      target="_blank"
                      rel="noopener"
                    >
                      <span class="card__ico"
                        ><pp-icon [name]="c.icon" [size]="16"
                      /></span>
                      <b>{{ c.title }}</b>
                      <span>{{ c.desc }}</span>
                    </a>
                  }
                }
              </div>
              @if (!reference().length) {
                <p class="muted">No references match “{{ query() }}”.</p>
              }
            </section>

            <!-- Explanation -->
            <section id="explanation" class="grp">
              <h2><span class="k">Explanation</span> Understand how it fits</h2>
              <p>
                PayProbe runs <strong>three-phase</strong> suites — component
                health checks, integration pairs, then full end-to-end flows —
                so a failure points at the component or integration that
                regressed. The async worker engine resolves each step's
                <code>target</code> to an adapter; a
                <a routerLink="/connections">Connection</a>
                names a concrete endpoint, and the universal TCP adapter
                multiplexes many messages over one long-lived socket (correlated
                by STAN for ISO 8583, or an echoed header for HSM host
                commands).
              </p>
              <p class="muted">
                Docs follow the
                <a href="https://diataxis.fr/" target="_blank" rel="noopener"
                  >Diátaxis</a
                >
                framework. Runnable examples in
                <code>examples/scenarios/</code> double as tests (<code
                  >make test</code
                >) — executable documentation that fails the build if it drifts.
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
  changeDetection: ChangeDetectionStrategy.Eager,
  styles: [
    `
      :host {
        display: block;
        height: 100%;
        background: var(--color-bg);
        color: var(--color-text);
      }
      .docs {
        display: flex;
        flex-direction: column;
        height: 100%;
      }
      .docs__bar {
        padding: 12px 16px;
        border-bottom: 1px solid var(--color-border);
      }
      .docs__bar h1 {
        margin: 0;
        font-size: 16px;
      }
      .docs__body {
        flex: 1 1 auto;
        overflow: auto;
        padding: 18px;
        display: flex;
        flex-direction: column;
        gap: 26px;
      }
      .muted {
        color: var(--color-text-muted, var(--color-text));
        font-size: 13px;
      }
      .grp h2 {
        font-size: 15px;
        margin: 0 0 6px;
        display: flex;
        align-items: center;
        gap: 10px;
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
      .steps {
        margin: 8px 0 0;
        padding-left: 20px;
        line-height: 1.9;
        font-size: 14px;
      }
      .steps code,
      p code {
        font-family: var(--font-mono, ui-monospace, monospace);
        font-size: 0.88em;
        background: var(--color-node, transparent);
        padding: 1px 5px;
        border-radius: 4px;
      }
      .cards {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
        gap: 12px;
        margin-top: 10px;
      }
      .card {
        display: flex;
        flex-direction: column;
        gap: 4px;
        text-decoration: none;
        color: var(--color-text);
        border: 1px solid var(--color-border);
        border-radius: 10px;
        padding: 14px;
        background: var(--color-node, var(--color-bg));
      }
      .card:hover {
        border-color: var(--color-primary, var(--color-border));
      }
      .card b {
        font-size: 14px;
      }
      .card span {
        font-size: 12px;
        color: var(--color-text-muted, var(--color-text));
      }
      .card--api b {
        color: var(--color-primary, #58a6ff);
      }
      p {
        font-size: 14px;
        line-height: 1.6;
      }
      a {
        color: var(--color-primary, #58a6ff);
      }
      /* platform diagram (scoped to this component) */
      .diagram {
        margin-top: 12px;
        border: 1px solid var(--color-border);
        border-radius: 12px;
        background: var(--color-bg);
        padding: 12px;
        overflow-x: auto;
      }
      .diagram svg {
        width: 100%;
        min-width: 760px;
        height: auto;
        display: block;
      }
      .pa-ttl {
        fill: var(--color-text);
        font-size: 16px;
        font-weight: 700;
      }
      .pa-box {
        fill: var(--color-node, var(--color-bg));
        stroke: var(--color-border);
        stroke-width: 1;
      }
      .pa-key {
        stroke: var(--color-primary, #58a6ff);
        stroke-width: 1.5;
      }
      .pa-t {
        fill: var(--color-text);
        font-size: 13px;
        font-weight: 600;
      }
      .pa-s {
        fill: var(--color-text-muted, var(--color-text));
        opacity: 0.7;
        font-size: 10px;
      }
      .pa-h {
        fill: var(--color-primary, #58a6ff);
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.05em;
      }
      .pa-edge {
        stroke: var(--color-border);
        stroke-width: 1.5;
        fill: none;
      }
      .pa-acc {
        stroke: var(--color-primary, #58a6ff);
        stroke-width: 1.8;
        fill: none;
      }
      .pa-arh {
        fill: var(--color-border);
      }
      .pa-arha {
        fill: var(--color-primary, #58a6ff);
      }
      .pa-el {
        fill: var(--color-text-muted, var(--color-text));
        opacity: 0.75;
        font-size: 9px;
      }
      .pa-ela {
        fill: var(--color-primary, #58a6ff);
        font-size: 9px;
      }
      .pa-pill {
        fill: var(--color-bg);
        stroke: var(--color-border);
        stroke-width: 1;
      }
      .pa-p {
        fill: var(--color-text);
        font-size: 10px;
        font-family: var(--font-mono, ui-monospace, monospace);
      }
      /* -- renovated docs layout -- */
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
      .hero h1 {
        font-size: 22px;
        margin: 2px 0 6px;
      }
      .hero p {
        font-size: 14.5px;
        max-width: 760px;
        line-height: 1.6;
      }
      .toggle {
        margin-left: auto;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
        background: transparent;
        border: 1px solid var(--color-border);
        border-radius: 7px;
        padding: 4px 10px;
        color: var(--color-text-muted, var(--color-text));
      }
      .toggle:hover {
        border-color: var(--color-primary, #58a6ff);
        color: var(--color-text);
      }
      .search {
        display: flex;
        align-items: center;
        gap: 8px;
        margin: 4px 0 12px;
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
      }
      .card {
        transition:
          border-color 0.12s ease,
          transform 0.12s ease;
      }
      .card:hover {
        transform: translateY(-1px);
      }
      .card__ico {
        width: 30px;
        height: 30px;
        border-radius: 8px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        background: var(--color-bg);
        border: 1px solid var(--color-border);
        color: var(--color-primary, #58a6ff);
        margin-bottom: 6px;
      }
    `,
  ],
})
export class DocsComponent {
  private readonly cfg = inject(RuntimeConfigService);
  readonly scenarioRef = `${this.cfg.scenarioApiBase}/reference`;
  readonly orchestratorRef = `${this.cfg.runApiBase}/reference`;

  readonly query = signal("");
  readonly diagramOpen = signal(false);

  readonly toc: TocItem[] = [
    { id: "platform", label: "Platform at a glance" },
    { id: "tutorial", label: "Tutorial" },
    { id: "howto", label: "How-to guides" },
    { id: "reference", label: "Reference" },
    { id: "explanation", label: "Explanation" },
  ];

  private readonly howtoCards: DocCard[] = [
    {
      title: "Configure a connection",
      icon: "send",
      route: "/connections",
      desc: "Define a switch/HSM endpoint, then test it live.",
    },
    {
      title: "Author a starter flow",
      icon: "flow",
      route: "/starter-flows",
      desc: "Build a reusable pre-wired chain for the editor palette.",
    },
    {
      title: "Decode an ISO 8583 message",
      icon: "search",
      route: "/inspector",
      desc: "Bitmap, per-field validation, EMV DE 55 TLV.",
    },
    {
      title: "Drive an HSM (payShield 10K)",
      icon: "cpu",
      route: "/docs/payshield",
      desc: "Generate & verify CVVs, translate PINs, MAC, EMV — vs the HSM simulator.",
    },
    {
      title: "Be the host, switch or HSM",
      icon: "layers",
      route: "/simulators",
      desc: "Stand up a rules-driven responder or a payShield HSM, no real system.",
    },
    {
      title: "Run a load test",
      icon: "zap",
      route: "/load",
      desc: "Steady / ramp / spike / soak across the worker fleet.",
    },
    {
      title: "Manage a message format",
      icon: "list",
      route: "/message-formats",
      desc: "Edit the DE table used to pack/parse messages.",
    },
    {
      title: "Add a data table",
      icon: "layers",
      route: "/tables",
      desc: "Card profiles / BIN ranges for data-driven runs.",
    },
    {
      title: "Browse adapters",
      icon: "flow",
      route: "/adapters",
      desc: "What each adapter type and protocol does.",
    },
    {
      title: "Check system & config",
      icon: "settings",
      route: "/settings",
      desc: "Auth, code sandbox, durability and service health in Settings → System.",
    },
  ];

  readonly howto = computed(() => this.match(this.howtoCards));
  readonly reference = computed(() => this.match(this.referenceCards));

  private get referenceCards(): DocCard[] {
    return [
      {
        title: "Scenario Service API ↗",
        icon: "book",
        href: this.scenarioRef,
        desc: "Interactive reference (Scalar) — scenarios, catalog, formats, ISO 8583.",
      },
      {
        title: "Orchestrator API ↗",
        icon: "book",
        href: this.orchestratorRef,
        desc: "Interactive reference — runs, reports, connection test.",
      },
      {
        title: "Step catalog",
        icon: "list",
        route: "/step-types",
        desc: "Every target and action available in the editor.",
      },
      {
        title: "ISO 8583 field reference",
        icon: "search",
        route: "/inspector",
        desc: "Analyze/build with type + length rules.",
      },
      {
        title: "payShield 10K HSM",
        icon: "cpu",
        route: "/docs/payshield",
        desc: "Config, every host command, error codes, key types & quick start.",
      },
    ];
  }

  private match(cards: DocCard[]): DocCard[] {
    const q = this.query().trim().toLowerCase();
    if (!q) return cards;
    return cards.filter((c) =>
      `${c.title} ${c.desc}`.toLowerCase().includes(q),
    );
  }

  onSearch(ev: Event): void {
    this.query.set((ev.target as HTMLInputElement).value);
  }
}
