import { createContext, useContext } from "react";
import type { AuthSession, UserRole } from "./api/generated";

export type AuthContextValue = {
  session: AuthSession | null;
  isLoading: boolean;
  error: Error | null;
  role: UserRole;
  can: (minimum: UserRole) => boolean;
};

export const AuthContext = createContext<AuthContextValue | null>(null);

export const useAuth = (): AuthContextValue => {
  const context = useContext(AuthContext);
  if (context === null) throw new Error("useAuth must be used inside AuthProvider");
  return context;
};
