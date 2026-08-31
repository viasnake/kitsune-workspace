import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import type { RunStatus } from "./api/generated";
import { AppShell } from "./components/app-shell";
import { AgentDetailPage } from "./pages/agent-detail-page";
import { AgentsPage } from "./pages/agents-page";
import { AuditPage } from "./pages/audit-page";
import { DashboardPage } from "./pages/dashboard-page";
import { NotFoundPage } from "./pages/not-found-page";
import { RunDetailPage } from "./pages/run-detail-page";
import { RunsPage } from "./pages/runs-page";
import { SchedulesPage } from "./pages/schedules-page";
import { ErrorState } from "./components/ui";
import { isListedValue, RUN_STATUSES } from "./utils";

const rootRoute = createRootRoute({
  component: AppShell,
  notFoundComponent: NotFoundPage,
  errorComponent: ({ error, reset }) => (
    <ErrorState error={error} retry={reset} />
  ),
});

const dashboardRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: DashboardPage });
const agentsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/agents", component: AgentsPage });
const agentDetailRoute = createRoute({ getParentRoute: () => rootRoute, path: "/agents/$agentId", component: AgentDetailPage });
const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs",
  component: RunsPage,
  validateSearch: (search: Record<string, unknown>): { agent?: string; status?: RunStatus } => {
    const agent = typeof search.agent === "string" && search.agent.length > 0 ? search.agent : undefined;
    const status = typeof search.status === "string" && isListedValue(RUN_STATUSES, search.status) ? search.status : undefined;
    return {
      ...(agent === undefined ? {} : { agent }),
      ...(status === undefined ? {} : { status }),
    };
  },
});
const runDetailRoute = createRoute({ getParentRoute: () => rootRoute, path: "/runs/$runId", component: RunDetailPage });
const schedulesRoute = createRoute({ getParentRoute: () => rootRoute, path: "/schedules", component: SchedulesPage });
const auditRoute = createRoute({ getParentRoute: () => rootRoute, path: "/audit", component: AuditPage });

const routeTree = rootRoute.addChildren([
  dashboardRoute,
  agentsRoute,
  agentDetailRoute,
  runsRoute,
  runDetailRoute,
  schedulesRoute,
  auditRoute,
]);

export const router = createRouter({
  routeTree,
  defaultPreload: "intent",
  defaultPreloadStaleTime: 0,
  scrollRestoration: true,
});

declare module "@tanstack/react-router" {
  // Module augmentation requires an interface with this exact name.
  // eslint-disable-next-line @typescript-eslint/consistent-type-definitions
  interface Register {
    router: typeof router;
  }
}
