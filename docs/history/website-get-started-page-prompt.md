# Prompt for Opus — payprobe.io Get Started page

Copy everything below the line into Claude inside the payprobe.io website project.

---

You are a senior frontend engineer working in the payprobe.io website codebase. Before writing anything, inspect the existing project and match its conventions exactly: framework, routing, styling system, components (nav, footer, section layouts, cards, code blocks), typography, and dark/light theming. The new page must look native to the site — reuse existing components wherever possible; do not introduce new dependencies.

## Task

Two changes:

1. **Create a new page at `/platform/get-started`** titled **"Get Started with the PayProbe Platform"** that shows how to run the whole platform locally with Docker Compose and links to the GitHub source.
2. **Repoint the Platform page CTA.** On `/platform`, the hero currently has a "Request access" CTA linking to the `/about` contact section. Rename it to **"Get started"** and link it to `/platform/get-started`. If the same "Request access" CTA appears in the closing CTA section or anywhere else on `/platform`, update those instances the same way. Do not touch any other contact links on the site.

The platform is source-available (PolyForm Noncommercial) at **https://github.com/pay-probe/payprobe** — self-serve, no sign-up, no gate. The page's job is to get a developer from zero to a running platform in one copy-paste.

## Page content (use exactly this; do not invent commands, flags, ports, or requirements)

### Hero / intro

Short headline + one line: run the entire platform — portal, orchestrator, scenario service, postgres, redis — on your laptop with Docker Compose. Mock mode by default: no payment hardware, secrets, or local toolchain required. Prominent GitHub link/button to https://github.com/pay-probe/payprobe (use the site's existing external-link or GitHub button pattern if one exists).

### Prerequisites

Docker with the Compose plugin, and Git. That's all.

### Option 1 — Prebuilt images (fastest, no build)

Pulls published images from Docker Hub; still run it from a checkout of the repo (the compose file mounts the nginx routing config and example seeds from the repo).

```bash
# 1. Clone the repository
git clone https://github.com/pay-probe/payprobe.git
cd payprobe

# 2. Start the whole platform from published images
docker compose -f infra/docker/docker-compose.hub.yml up -d
#    (or: make up-hub)

# 3. Open the portal
open http://localhost:8080
```

Note under the block: pin a version with `PAYPROBE_TAG=v0.1.0 docker compose -f infra/docker/docker-compose.hub.yml up -d` (default `latest`). Stop with `docker compose -f infra/docker/docker-compose.hub.yml down` (or `make down-hub`).

### Option 2 — Build from source

```bash
git clone https://github.com/pay-probe/payprobe.git
cd payprobe

# Build and start the whole stack
docker compose -f infra/docker/docker-compose.yml up --build

# Open the portal
open http://localhost:8080
```

Note under the block: tear down with `docker compose -f infra/docker/docker-compose.yml down` (add `-v` to also drop the postgres & redis volumes).

### What you get

A small table (reuse the site's table or definition-list pattern):

| Service | URL |
|---|---|
| Portal (UI + nginx proxy) | http://localhost:8080 |
| Orchestrator API | http://localhost:8100 |
| Scenario service API | http://localhost:8000 |

### First run

One short paragraph: open the portal, start a mock run from the Run Monitor, and watch steps stream in live. Point to the repo's README and `infra/docker/README.md` for configuration (`.env.example` → `.env`) and production notes — link both into the GitHub repo rather than duplicating their content.

### License note

One line: PayProbe is source-available under the PolyForm Noncommercial license — free for personal, research, and internal noncommercial use. Link the license file in the repo.

### Closing CTA

Consistent with the rest of the site: GitHub button (star/view source) + a secondary link back to `/platform`.

## SEO

Page title "Get Started — Run the PayProbe Platform with Docker Compose", meta description (~155 chars) covering: run a payment-network digital twin locally with Docker Compose, prebuilt images or build from source, open source on GitHub. OG tags matching site patterns.

## Constraints

- Copy-paste correctness is the whole point: every command must appear exactly as written above, in the site's existing code-block component (with copy button if the site has one).
- Responsive, accessible (semantic headings, code blocks readable in both themes).
- No invented numbers, requirements, or steps; no screenshots needed on this page.
- Add the page to the sitemap/routing the same way other pages are registered; it does **not** need its own nav or footer entry — it is reached from the `/platform` CTAs (add a footer link only if the footer already has a natural "Get started"/docs slot).
- After building, verify: `/platform/get-started` renders, both CTAs on `/platform` now read "Get started" and navigate there, and no remaining link on `/platform` points to the `/about` contact for access requests.
