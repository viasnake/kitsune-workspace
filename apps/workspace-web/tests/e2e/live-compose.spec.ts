import { expect, test } from "@playwright/test";

const composeE2e = process.env.KITSUNE_COMPOSE_E2E === "1";

test.describe("Docker Compose live Workspace UI", () => {
  test.skip(!composeE2e, "Set KITSUNE_COMPOSE_E2E=1 to run against the Compose Workspace.");

  test("starts a Manual Run from Agent detail and observes its successful output", async ({ page }) => {
    test.setTimeout(120_000);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "運用ダッシュボード" })).toBeVisible({ timeout: 30_000 });
    await page.getByRole("link", { name: "Agent", exact: true }).click();

    const agentLink = page.getByRole("link", { name: /Managed Resident Agent managed-resident-agent/ }).first();
    await expect(async () => {
      await page.reload();
      await expect(agentLink).toBeVisible();
    }).toPass({ timeout: 90_000, intervals: [1_000, 2_000, 5_000] });
    await agentLink.click();

    const messageInput = page.getByLabel(/^Message/);
    await expect(async () => {
      await page.reload();
      await expect(messageInput).toBeVisible();
    }).toPass({ timeout: 90_000, intervals: [1_000, 2_000, 5_000] });
    await messageInput.fill("compose-live-ui");
    await page.getByRole("button", { name: "Run を開始" }).click();

    await expect(page).toHaveURL(/\/runs\/[^/]+$/, { timeout: 30_000 });
    await expect(page.getByRole("heading", { name: /^Run / })).toBeVisible();
    await expect(page.getByText("成功", { exact: true }).first()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText(/Processed: compose-live-ui/)).toBeVisible();
  });
});
