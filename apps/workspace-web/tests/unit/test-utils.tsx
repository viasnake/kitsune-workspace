import type { ReactNode } from "react";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { AuthProvider } from "../../src/auth";

export const renderRoute = ({
  element,
  path,
  entry,
  additionalRoutes = [],
}: {
  element: ReactNode;
  path: string;
  entry: string;
  additionalRoutes?: { path: string; element: ReactNode }[];
}) => {
  const rootRoute = createRootRoute({ component: Outlet });
  const primaryRoute = createRoute({ getParentRoute: () => rootRoute, path, component: () => element });
  const routes = [
    primaryRoute,
    ...additionalRoutes.map((route) =>
      createRoute({ getParentRoute: () => rootRoute, path: route.path, component: () => route.element }),
    ),
  ];
  const history = createMemoryHistory({ initialEntries: [entry] });
  const testRouter = createRouter({ routeTree: rootRoute.addChildren(routes), history });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={testRouter} />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { ...result, queryClient, router: testRouter, user: userEvent.setup() };
};
