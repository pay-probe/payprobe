import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the portal's golden-path e2e.
 *
 * By default it builds + serves the portal on :4200 and runs Chromium against
 * it. The functional flows (run, load test) need the backend stack too; point
 * the portal's environment at a running orchestrator/scenario-service (or the
 * mock docker-compose) and set E2E_BASE_URL to skip the built-in webServer.
 *
 *   npm run e2e:install     # one-time: download the browser
 *   npm run e2e             # build+serve+test
 *   E2E_BASE_URL=http://localhost:4200 npm run e2e   # test an already-running app
 */
const baseURL = process.env.E2E_BASE_URL || "http://localhost:4200";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  // Only start our own server when not pointed at an external one.
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: "npm run start -- --port 4200",
        url: "http://localhost:4200",
        timeout: 120_000,
        reuseExistingServer: !process.env.CI,
      },
});
