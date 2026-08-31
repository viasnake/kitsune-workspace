import { expect, test, type Page, type Route } from "@playwright/test";
import { agentDetail, agentSummary, runEvent, runningRun } from "../unit/fixtures";

type MockOptions = {
  role?: "viewer" | "operator" | "admin";
  failAgents?: boolean;
  unauthenticated?: boolean;
};

const health = {
  status: "healthy",
  database: "healthy",
  instance_lock: "held",
  scheduler: "running",
  runtimes: { ready: 1, unhealthy: 0, lost: 0 },
  timestamp: "2026-08-24T01:00:00Z",
};

const dashboard = {
  agents: { total: 1, running: 1, stopped: 0, failed: 0 },
  runs: { active: 1, failed: 0 },
  usage_today: {
    request_count: 3,
    input_tokens: 800,
    output_tokens: 200,
    total_tokens: 1_000,
    estimated_cost: "0.020000000000000000",
    currency: "USD",
  },
  recent_anomalies: [],
  next_schedules: [],
};

const fulfillJson = (route: Route, json: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(json) });

async function mockWorkspaceApi(page: Page, options: MockOptions = {}) {
  const role = options.role ?? "operator";
  const displayRole = role === "viewer" ? "Viewer" : role === "operator" ? "Operator" : "Admin";
  let authenticated = options.unauthenticated !== true;
  await page.route(/^https?:\/\/[^/]+\/api\/.*/, async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());

    if (pathname === "/api/stream") {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache" },
        body: ": connected\n\n",
      });
    }
    if (pathname === "/api/auth/me") {
      if (!authenticated) {
        return fulfillJson(route, { title: "Unauthorized", status: 401, detail: "Authentication required" }, 401);
      }
      return fulfillJson(route, {
        subject: `${role}@example.invalid`,
        name: `Kitsune ${displayRole}`,
        roles: [role],
        csrf_token: `${role}-csrf`,
      });
    }
    if (pathname === "/api/auth/login") {
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: "<!doctype html><title>OIDC handoff</title><h1>OIDC handoff</h1>",
      });
    }
    if (pathname === "/api/auth/logout" && request.method() === "POST") {
      authenticated = false;
      return route.fulfill({ status: 204 });
    }
    if (pathname === "/api/health") return fulfillJson(route, health);
    if (pathname === "/api/dashboard") return fulfillJson(route, dashboard);
    if (pathname === "/api/agents" && options.failAgents === true) {
      return fulfillJson(
        route,
        { type: "about:blank", title: "Service Unavailable", status: 503, detail: "Agent registry is unavailable" },
        503,
      );
    }
    if (pathname === "/api/agents") return fulfillJson(route, [agentSummary]);
    if (pathname === "/api/agents/sre-agent/runs" && request.method() === "POST") return fulfillJson(route, runningRun, 201);
    if (pathname === "/api/agents/sre-agent") return fulfillJson(route, agentDetail);
    if (pathname === `/api/runs/${runningRun.run_id}/events`) return fulfillJson(route, [runEvent]);
    if (pathname === `/api/runs/${runningRun.run_id}/usage`) return fulfillJson(route, []);
    if (pathname === `/api/runs/${runningRun.run_id}/cancel` && request.method() === "POST") {
      return fulfillJson(route, { ...runningRun, status: "cancelled", ended_at: "2026-08-24T01:01:00Z" });
    }
    if (pathname === `/api/runs/${runningRun.run_id}`) return fulfillJson(route, runningRun);
    if (pathname === "/api/runs") return fulfillJson(route, [runningRun]);
    if (pathname === "/api/schedules" || pathname === "/api/audit") return fulfillJson(route, []);
    if (pathname === "/api/admin/reload" && request.method() === "POST") {
      return fulfillJson(route, { loaded: 1, added: [], updated: [], removed: [], unchanged: ["sre-agent"], loaded_at: "2026-08-24T01:00:00Z" });
    }

    return fulfillJson(route, { title: "Not Found", status: 404, detail: `No mock for ${request.method()} ${pathname}` }, 404);
  });
}

test("operator can inspect an Agent and start a schema-driven Run", async ({ page }) => {
  await mockWorkspaceApi(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "運用ダッシュボード" })).toBeVisible();
  await expect(page.getByLabel("主要指標")).toContainText("Active Run");
  await expect(page.getByLabel("主要指標")).toContainText("dispatching / running");
  await expect(page.getByLabel("主要指標")).toContainText("保存中の履歴");
  await page.getByRole("link", { name: "Agent", exact: true }).click();
  await page.getByRole("link", { name: /SRE Agent sre-agent/ }).click();

  await expect(page.getByRole("heading", { name: "SRE Agent" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop" })).toBeEnabled();
  await page.getByLabel(/調査内容/).fill("database latency");
  await page.getByRole("button", { name: "Run を開始" }).click();

  await expect(page).toHaveURL(new RegExp(`/runs/${runningRun.run_id}$`));
  await expect(page.getByRole("heading", { name: `Run ${runningRun.run_id}` })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Run Tree" })).toBeVisible();
  await expect(page.getByText("kitsune.run.progress")).toBeVisible();
});

test("viewer sees read-only controls", async ({ page }) => {
  await mockWorkspaceApi(page, { role: "viewer" });
  await page.goto("/agents/sre-agent");

  await expect(page.getByRole("heading", { name: "SRE Agent" })).toBeVisible();
  await expect(page.getByText("Runtime 操作は operator 以上")).toBeVisible();
  await expect(page.getByText(/手動実行には operator 権限が必要/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run を開始" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Manifest 再読込" })).toHaveCount(0);
});

test("API failures are recoverable and include server detail", async ({ page }) => {
  await mockWorkspaceApi(page, { failAgents: true });
  await page.goto("/agents");

  await expect(page.getByRole("alert")).toContainText("Agent registry is unavailable");
  await expect(page.getByRole("button", { name: "再試行" })).toBeEnabled();
});

test("unauthenticated users can enter the OIDC login flow", async ({ page }) => {
  await mockWorkspaceApi(page, { unauthenticated: true });
  await page.goto("/agents");

  await expect(page.getByRole("heading", { name: "認証が必要です" })).toBeVisible();
  const login = page.getByRole("link", { name: "OIDC でログイン" });
  await expect(login).toHaveAttribute("href", "/api/auth/login?return_to=%2Fagents");
  await login.click();
  await expect(page).toHaveURL(/\/api\/auth\/login\?return_to=%2Fagents$/);
  await expect(page.getByRole("heading", { name: "OIDC handoff" })).toBeVisible();
});

test("authenticated users can end the Workspace session", async ({ page }) => {
  await mockWorkspaceApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "ログアウト" }).click();
  await expect(page.getByRole("heading", { name: "認証が必要です" })).toBeVisible();
});
