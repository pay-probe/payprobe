import { test, expect } from "@playwright/test";

/**
 * Golden-path UI smoke tests — run against just the portal (no backend needed).
 * They assert the high-traffic pages render and the Load Test form reacts to
 * the profile type. Backend-dependent flows live in full-flows.spec.ts.
 */

// The shell sits behind the auth guard, which only checks for a token in
// localStorage. Seed a fake session before every navigation so these
// backend-free smoke tests reach the authenticated pages (they render on mock
// data — no real token validation happens). Keys mirror AuthService.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("pp.auth.token", "e2e-fake-token");
    localStorage.setItem(
      "pp.auth.user",
      JSON.stringify({
        username: "e2e",
        roles: ["admin"],
        project_ids: [],
      }),
    );
  });
});

test("dashboard loads and the sidenav reaches the key tools", async ({
  page,
}) => {
  await page.goto("/dashboard");
  await expect(page.locator("pp-sidenav, app-sidenav")).toBeVisible();
  // the two-level nav exposes Scenarios and the Load Test tool
  await expect(page.getByText("Scenarios", { exact: false })).toBeVisible();
});

test("Load Test page renders and adapts to the profile type", async ({
  page,
}) => {
  await page.goto("/load");
  // The shell topbar renders the route title in its own <h1 class="page-title">;
  // the page component itself opens on the "Workload" configuration card. Assert
  // that page-specific heading (scoped to the main landmark) to prove the Load
  // component mounted, not just the route banner.
  await expect(
    page.getByRole("main").getByRole("heading", { name: "Workload" }),
  ).toBeVisible();

  // The profile type is driven by one-click preset buttons (there is no <select>).
  // The page defaults to the "Sustained" steady preset, so a Target TPS field is
  // present out of the gate.
  await expect(page.locator('input[name="tps"]')).toBeVisible();

  // Selecting the Soak preset flips the profile to a soak run: connection +
  // heartbeat fields appear and the steady TPS field goes away.
  await page.getByRole("button", { name: /^Soak:/ }).click();
  await expect(page.locator('input[name="conn"]')).toBeVisible();
  await expect(page.locator('input[name="hb"]')).toBeVisible();
  await expect(page.locator('input[name="tps"]')).toBeHidden();

  // the Start button is the primary call to action
  await expect(
    page.getByRole("button", { name: /Start load test/i }),
  ).toBeVisible();

  // the run-history panel is present (even if empty)
  await expect(page.getByRole("heading", { name: "History" })).toBeVisible();
});

test("Docs page shows the platform architecture diagram", async ({ page }) => {
  await page.goto("/docs");
  await expect(
    page.getByRole("heading", { name: /Platform at a glance/i }),
  ).toBeVisible();
  // The architecture diagram is collapsed by default behind a toggle — open it.
  await page.getByRole("button", { name: /Show diagram/i }).click();
  // the inline SVG diagram is present and labelled for a11y
  await expect(page.locator("svg[role='img'] title")).toHaveText(
    /Platform architecture/i,
  );
});
