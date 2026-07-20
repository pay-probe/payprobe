# Prompt for Opus 4.8 — payprobe.io Platform page

Copy everything below the line into Claude Opus 4.8 inside the payprobe.io website project.

---

You are a senior frontend engineer working in the payprobe.io website codebase. Before writing anything, inspect the existing project and match its conventions exactly: framework, routing, styling system, components (nav, footer, section layouts, cards, code blocks), typography, and dark/light theming. The new page must look native to the site — reuse existing components wherever possible; do not introduce new dependencies.

## Task

Create a new page at `/platform` titled **"PayProbe Platform"** and add it to the main navigation and footer (Company or Product column). The existing `/product` page stays untouched.

The page presents the PayProbe Platform — a self-hosted payment-systems testing platform, distinct from the site's 82 free browser tools. Frame it as: *the free tools help you decode and compute; the Platform lets you build and run an entire payment network on your laptop — a digital twin of a payment network.*

## What the platform actually is (use this content; do not invent features or metrics)

Describe these capabilities, each as a page section. Keep the site's developer-first tone: concrete, technical, no marketing fluff.

1. **Visual scenario constructor.** Test scenarios are built on a canvas: drag protocol steps, wire them with edges that mean "then". Steps cover ISO 8583, HSM commands, REST calls, assertions, variables, loops, branching. Scenarios are validated before they run.
2. **Protocol simulators.** Built-in simulators for ISO 8583 hosts, Thales payShield HSM, VISA Base I, CyberSource REST, and NATS messaging. Start one in seconds, point your system under test at it, script its responses — including EMV/ARQC cryptography.
3. **Networks — the digital twin.** Author a whole network of listening participants on a canvas: issuers, acquirers, switches, HSMs, simulators, wired together with edges that mean "sends traffic to". One click starts everything in dependency order across one machine or a distributed fleet. **This is the flagship section — it carries the video demo.**
4. **Live network map.** While a network runs, an animated map shows traffic flowing between participants in real time, with per-participant health and message traces you can drill into.
5. **Distributed load generation.** Scale traffic across a fleet of load workers; compare load runs, track latency percentiles and throughput.
6. **Chaos & resilience certification.** Inject faults (latency, drops, error storms) into simulators mid-run, run scripted resilience campaigns, and get pass/fail certification against defined gates.
7. **Run reports & Go/No-Go sign-off.** Every run produces a report with gates and provenance; releases get an explicit approve/reject sign-off trail.
8. **Built-in AI assistant + MCP server.** An assistant that can build scenarios, connections, and networks for you — every write it makes is journalled and reversible. An MCP server exposes the whole platform to external agents like Claude.
9. **Advisory ML insights.** Failure categorization, explanation, and outcome prediction on runs — read-only and advise-only, never gating.
10. **Supporting registry.** Message format editor, environments with per-environment override matrices, encrypted secrets vault, card/terminal pools, test keys.

## Page structure

- **Hero:** headline + subline positioning the digital-twin idea, two CTAs ("Watch the demo" → scrolls to video; "Request access" → link to `/about` contact). A hero visual placeholder (network canvas screenshot).
- **Video section** (prominent, after hero or after the Networks section): HTML5 `<video>` — `controls`, `preload="none"`, `poster`, lazy; do not autoplay with sound. Caption: "A network of participants starting up and processing live traffic on the network map."
- **Feature sections:** alternate text-left/image-right using the screenshot placeholders below.
- **Closing CTA** consistent with the rest of the site.
- **SEO:** page title "PayProbe Platform — A Digital Twin of a Payment Network", meta description (~155 chars) summarizing scenario building, simulators, networks, load, and chaos testing; OG tags matching site patterns.

## Assets — placeholders now, real files later

Reference all media from `/assets/platform/` using these exact filenames. Until the files exist, render a styled placeholder (gray panel with the filename and alt text) so the page builds and looks intentional. Give every image a descriptive `alt`.

| File | Shows |
|---|---|
| `scenario-canvas.png` | Scenario constructor canvas with a multi-step ISO 8583 flow |
| `simulators.png` | Simulators page with running payShield / ISO 8583 / VISA simulators |
| `network-canvas.png` | Network editor canvas with participants wired together |
| `network-map-live.png` | Live network map during a run (also the video `poster`) |
| `load-dashboard.png` | Load run dashboard with latency/throughput charts |
| `chaos-resilience.png` | Resilience/chaos run view |
| `run-report.png` | Run report with gates and sign-off |
| `network-map-demo.mp4` | 30–60s screen recording of a network starting and the map animating live traffic |

## Constraints

- Responsive, accessible (semantic headings, alt text, keyboard-reachable video controls).
- No invented numbers, customers, logos, or benchmarks.
- Reuse the site's existing section/card/CTA components; if none fit, follow the closest existing pattern.
- After building, verify the page renders, nav/footer links work, and placeholders display cleanly.

---

## Shot list (for you, David — capture from the portal at :4200, then drop files into `/assets/platform/`)

1. `scenario-canvas.png` — open a rich scenario (multi-branch ISO 8583 flow) in the constructor.
2. `simulators.png` — Simulators page with 2–3 simulators running (payShield + ISO 8583 look best).
3. `network-canvas.png` — a showcase network in the network editor (`install_showcase` gives a good one).
4. `network-map-live.png` — same network running, map animated; use as video poster too.
5. `load-dashboard.png` — a completed load run with charts.
6. `chaos-resilience.png` — a resilience run or chaos storm view.
7. `run-report.png` — a run report showing gates passed + sign-off.
8. `network-map-demo.mp4` — screen-record (⌘⇧5, crop to the map): start the network flow, let the map animate startup + live traffic ~30–60s, stop recording. Remember trace capture is off by default — resume capture first if you want traces visible.

Capture at 2x/retina, light or dark theme matching the site, browser chrome cropped out.
