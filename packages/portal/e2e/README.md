# Portal e2e (Playwright)

Golden-path browser tests for the portal.

## Run

Playwright is intentionally **not** a `package.json` dependency — it's a
test-only tool and keeping it out means it never enters the portal's production
Docker build (`npm ci`). `e2e:install` adds it with `--no-save` and downloads the
browser.

```bash
cd packages/portal
npm ci
npm run e2e:install      # adds @playwright/test (--no-save) + downloads Chromium
npm run e2e              # build + serve on :4200 + run the UI smoke specs
```

`golden-paths.spec.ts` runs against **just the portal** (no backend): it checks
the dashboard, the Load Test form's reactivity, the reopen list, and the Docs
platform diagram.

`full-flows.spec.ts` exercises the **whole stack** (author→run→report,
configure→load→stop, diagnose). It's skipped unless the backend is up:

```bash
# bring up the mock stack, then:
E2E_FULL=1 npm run e2e
```

Point the portal at an already-running instance to skip the built-in dev server:

```bash
E2E_BASE_URL=https://staging.example npm run e2e
```
