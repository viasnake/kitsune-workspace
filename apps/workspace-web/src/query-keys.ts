export const queryKeys = {
  session: ["session"] as const,
  dashboard: ["dashboard"] as const,
  health: ["health"] as const,
  agents: ["agents"] as const,
  agent: (agentId: string) => ["agent", agentId] as const,
  runs: (filters: Record<string, unknown> = {}) => ["runs", filters] as const,
  run: (runId: string) => ["run", runId] as const,
  runEvents: (runId: string) => ["run-events", runId] as const,
  runUsage: (runId: string) => ["run-usage", runId] as const,
  schedules: ["schedules"] as const,
  audit: ["audit"] as const,
  runtimeLogs: (runtimeInstanceId: string) => ["runtime-logs", runtimeInstanceId] as const,
};
