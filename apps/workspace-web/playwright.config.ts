import { defineConfig, devices } from "@playwright/test";

const composeE2e = process.env.KITSUNE_COMPOSE_E2E === "1";
const defaultBaseUrl = composeE2e ? "http://127.0.0.1:8080" : "http://127.0.0.1:4173";
const baseUrl = process.env.KITSUNE_E2E_URL?.replace(/\/$/, "") ?? defaultBaseUrl;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: "list",
  use: {
    baseURL: baseUrl,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  ...(composeE2e
    ? {}
    : {
        webServer: {
          command: "pnpm dev --host 127.0.0.1 --port 4173",
          url: "http://127.0.0.1:4173",
          reuseExistingServer: !process.env.CI,
        },
      }),
});
