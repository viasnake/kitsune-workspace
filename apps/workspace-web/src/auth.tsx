import { type ReactNode, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { workspaceApi } from "./api/generated";
import { AuthContext, type AuthContextValue } from "./auth-context";
import { queryKeys } from "./query-keys";
import { hasMinimumRole, highestRole } from "./utils";

export function AuthProvider({ children }: { children: ReactNode }) {
  const sessionQuery = useQuery({
    queryKey: queryKeys.session,
    queryFn: ({ signal }) => workspaceApi.getAuthSession(signal),
    staleTime: 5 * 60_000,
    retry: false,
  });

  const value = useMemo<AuthContextValue>(() => {
    const roles = sessionQuery.data?.roles ?? [];
    return {
      session: sessionQuery.data ?? null,
      isLoading: sessionQuery.isLoading,
      error: sessionQuery.error,
      role: highestRole(roles),
      can: (minimum) => hasMinimumRole(roles, minimum),
    };
  }, [sessionQuery.data, sessionQuery.error, sessionQuery.isLoading]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
