import { test, expect } from "@playwright/test";

/**
 * End-to-end golden paths that exercise the full stack (orchestrator +
 * scenario-service + a worker / mock environment). They're skipped by default
 * because they need the backend up; run them with the mock stack via:
 *
 *   E2E_FULL=1 npm run e2e
 *
 * Bring the stack up first (e.g. `docker compose -f infra/docker/docker-compose.mock.yml up`)
 * and point the portal's environment at it.
 */
const full = process.env.E2E_FULL === "1";

test.describe("full stack", () => {
  test.skip(!full, "set E2E_FULL=1 with the backend stack running");

  test("author → run → report", async ({ page }) => {
    // start a run from the run-monitor against the mock environment
    await page.goto("/run-monitor");
    await page.getByRole("button", { name: /Start run|Run/i }).first().click();
    // a run id / live status appears, then the run completes
    await expect(page.getByText(/running|passed|completed/i).first()).toBeVisible({
      timeout: 20_000,
    });
    // the run shows up in history
    await page.goto("/runs");
    await expect(page.locator("table, .runlist, .run-row").first()).toBeVisible();
  });

  test("configure → load test → stop", async ({ page }) => {
    await page.goto("/load");
    await page.locator('select[name="type"]').selectOption("steady");
    await page.locator('input[name="tps"]').fill("50");
    await page.locator('input[name="dur"]').fill("30");
    await page.getByRole("button", { name: /Start load test/i }).click();

    // live metrics appear (workers report, TPS/received update)
    await expect(page.getByText("TPS now")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/\d+\/\d+/)).toBeVisible(); // workers x/y

    // stop the run
    await page.getByRole("button", { name: /Stop/i }).click();
    await expect(page.getByRole("button", { name: /Start load test/i })).toBeVisible({
      timeout: 20_000,
    });
  });

  test("diagnose surfaces a cause when a run fails", async ({ page }) => {
    // drive against an unreachable target so every transaction errors
    await page.goto("/load");
    await page.locator('select[name="type"]').selectOption("steady");
    await page.locator('input[name="tps"]').fill("50");
    await page.locator('input[name="dur"]').fill("10");
    await page.getByRole("button", { name: /Start load test/i }).click();
    await page.getByRole("button", { name: /Diagnose this run/i }).click({ timeout: 20_000 });
    await expect(page.locator(".finding").first()).toBeVisible();
  });
});
