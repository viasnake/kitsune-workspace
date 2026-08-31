import createClient, { type Client } from "openapi-fetch";
import type { components, paths } from "./openapi.generated";
import type { JsonValue } from "../json";

type Schemas = components["schemas"];

export type UserRole = Schemas["UserRole"];
export type RuntimeMode = Schemas["RuntimeMode"];
export type RuntimeAdapter = Schemas["RuntimeAdapter"];
export type DesiredState = Schemas["DesiredState"];
export type RuntimeStatus = Schemas["RuntimeStatus"];
export type RunStatus = Schemas["RunStatus"];
export type RunSource = Schemas["RunSource"];
export type TriggerType = Schemas["TriggerType"];
export type Severity = Schemas["EventSeverity"];
export type OverlapPolicy = Schemas["OverlapPolicy"];
export type QueuePolicy = Schemas["QueuePolicy"];
export type ScheduleOutcome = Schemas["ScheduleOutcome"];
export type HealthStatus = Schemas["HealthStatus"];
export type SchedulerStatus = Schemas["SchedulerStatus"];
export type InstanceLockStatus = Schemas["InstanceLockStatus"];
export type OperationStatus = Schemas["OperationStatus"];
export type PluginStatus = Schemas["PluginStatus"];
export type LogSource = Schemas["LogSource"];
export type AuditActorType = Schemas["AuditActorType"];
export type AuditRole = Schemas["AuditRole"];
export type AuditOutcome = Schemas["AuditOutcome"];

export type AuthSession = Schemas["AuthMeResponse"];
export type UsageRecord = Schemas["UsageView"];
export type UsageSummary = Schemas["UsageSummary"];
export type RunSummary = Schemas["RunSummary"];
export type AgentSummary = Schemas["AgentSummary"];
export type AgentDescriptor = Schemas["AgentDescriptor"];
export type Handler = Schemas["HandlerView"];
export type Plugin = Schemas["PluginView"];
export type RuntimeInstance = Schemas["RuntimeView"];
export type Trigger = Schemas["TriggerView"];
export type Schedule = Schemas["ScheduleView"];
export type HealthCheck = Schemas["HealthCheckView"];
export type AgentHealth = Schemas["HealthDetail"];
export type AgentDetail = Schemas["AgentDetail"];
export type Run = Schemas["RunView"];
export type KitsuneEvent = Schemas["EventView"];
export type DashboardAnomaly = Schemas["AnomalyView"];
export type DashboardSummary = Schemas["DashboardResponse"];
export type AuditRecord = Schemas["AuditView"];
export type WorkspaceHealth = Schemas["WorkspaceHealthResponse"];
export type RuntimeLogs = Schemas["LogsResponse"];
export type OperationResponse = Schemas["OperationResponse"];
export type ManifestReloadResponse = Schemas["ManifestReloadResponse"];
export type CreateRunRequest = Schemas["RunCreateRequest"];
export type ListRunsParams = NonNullable<paths["/api/runs"]["get"]["parameters"]["query"]>;
export type RuntimeLogParams = NonNullable<
  paths["/api/runtime-instances/{runtime_instance_id}/logs"]["get"]["parameters"]["query"]
>;

export type ApiErrorDocument = {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  instance?: string;
  code?: string;
  errors?: unknown;
};

export type StreamEnvelope = {
  type: "run" | "runtime" | "event" | "reload";
  data: JsonValue;
  occurred_at: string;
};

export type WorkspaceApiOptions = {
  baseUrl?: string;
  fetch?: typeof globalThis.fetch;
};

type OpenApiResult<Data> =
  | { data: Data; response: Response }
  | { error: unknown; response: Response };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const hasOptionalString = (record: Record<string, unknown>, field: string): boolean =>
  record[field] === undefined || typeof record[field] === "string";

/** Return whether an unknown error body is a supported RFC 9457-style document. */
export const isApiErrorDocument = (value: unknown): value is ApiErrorDocument =>
  isRecord(value) &&
  hasOptionalString(value, "type") &&
  hasOptionalString(value, "title") &&
  (value.status === undefined || typeof value.status === "number") &&
  hasOptionalString(value, "detail") &&
  hasOptionalString(value, "instance") &&
  hasOptionalString(value, "code");

export class ApiError extends Error {
  readonly status: number;
  readonly document: ApiErrorDocument | null;
  readonly requestId: string | null;

  constructor(status: number, document: ApiErrorDocument | null, requestId: string | null) {
    super(document?.detail ?? document?.title ?? `API request failed with status ${String(status)}`);
    this.name = "ApiError";
    this.status = status;
    this.document = document;
    this.requestId = requestId;
  }
}

export class WorkspaceApi {
  readonly baseUrl: string;
  private readonly client: Client<paths>;

  constructor(options: WorkspaceApiOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "").replace(/\/$/, "");
    this.client = createClient<paths>({
      baseUrl: this.baseUrl,
      credentials: "same-origin",
      ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
    });
  }

  private async unwrap<Data>(resultPromise: Promise<OpenApiResult<Data>>): Promise<Data> {
    const result = await resultPromise;
    if ("data" in result) return result.data;
    const document = isApiErrorDocument(result.error) ? result.error : null;
    throw new ApiError(result.response.status, document, result.response.headers.get("x-request-id"));
  }

  getAuthSession(signal?: AbortSignal): Promise<AuthSession> {
    return this.unwrap(this.client.GET("/api/auth/me", { ...(signal === undefined ? {} : { signal }) }));
  }

  logout(csrfToken: string): Promise<void> {
    return this.unwrap(this.client.POST("/api/auth/logout", {
      headers: { "X-CSRF-Token": csrfToken },
    }));
  }

  getDashboard(signal?: AbortSignal): Promise<DashboardSummary> {
    return this.unwrap(this.client.GET("/api/dashboard", { ...(signal === undefined ? {} : { signal }) }));
  }

  getHealth(signal?: AbortSignal): Promise<WorkspaceHealth> {
    return this.unwrap(this.client.GET("/api/health", { ...(signal === undefined ? {} : { signal }) }));
  }

  listAgents(signal?: AbortSignal): Promise<AgentSummary[]> {
    return this.unwrap(this.client.GET("/api/agents", { ...(signal === undefined ? {} : { signal }) }));
  }

  getAgent(agentId: string, signal?: AbortSignal): Promise<AgentDetail> {
    return this.unwrap(this.client.GET("/api/agents/{agent_id}", {
      params: { path: { agent_id: agentId } },
      ...(signal === undefined ? {} : { signal }),
    }));
  }

  startAgent(agentId: string, csrfToken: string): Promise<OperationResponse> {
    return this.unwrap(this.client.POST("/api/agents/{agent_id}/start", {
      params: { path: { agent_id: agentId } },
      headers: { "X-CSRF-Token": csrfToken },
    }));
  }

  stopAgent(agentId: string, csrfToken: string): Promise<OperationResponse> {
    return this.unwrap(this.client.POST("/api/agents/{agent_id}/stop", {
      params: { path: { agent_id: agentId } },
      headers: { "X-CSRF-Token": csrfToken },
    }));
  }

  restartAgent(agentId: string, csrfToken: string): Promise<OperationResponse> {
    return this.unwrap(this.client.POST("/api/agents/{agent_id}/restart", {
      params: { path: { agent_id: agentId } },
      headers: { "X-CSRF-Token": csrfToken },
    }));
  }

  createRun(agentId: string, request: CreateRunRequest, csrfToken: string): Promise<Run> {
    return this.unwrap(this.client.POST("/api/agents/{agent_id}/runs", {
      params: { path: { agent_id: agentId } },
      headers: { "X-CSRF-Token": csrfToken },
      body: request,
    }));
  }

  listRuns(params: ListRunsParams = {}, signal?: AbortSignal): Promise<Run[]> {
    return this.unwrap(this.client.GET("/api/runs", {
      params: { query: params },
      ...(signal === undefined ? {} : { signal }),
    }));
  }

  getRun(runId: string, signal?: AbortSignal): Promise<Run> {
    return this.unwrap(this.client.GET("/api/runs/{run_id}", {
      params: { path: { run_id: runId } },
      ...(signal === undefined ? {} : { signal }),
    }));
  }

  cancelRun(runId: string, csrfToken: string): Promise<Run> {
    return this.unwrap(this.client.POST("/api/runs/{run_id}/cancel", {
      params: { path: { run_id: runId } },
      headers: { "X-CSRF-Token": csrfToken },
    }));
  }

  getRunEvents(runId: string, signal?: AbortSignal): Promise<KitsuneEvent[]> {
    return this.unwrap(this.client.GET("/api/runs/{run_id}/events", {
      params: { path: { run_id: runId } },
      ...(signal === undefined ? {} : { signal }),
    }));
  }

  getRunUsage(runId: string, signal?: AbortSignal): Promise<UsageRecord[]> {
    return this.unwrap(this.client.GET("/api/runs/{run_id}/usage", {
      params: { path: { run_id: runId } },
      ...(signal === undefined ? {} : { signal }),
    }));
  }

  listSchedules(signal?: AbortSignal): Promise<Schedule[]> {
    return this.unwrap(this.client.GET("/api/schedules", { ...(signal === undefined ? {} : { signal }) }));
  }

  listAudit(signal?: AbortSignal): Promise<AuditRecord[]> {
    return this.unwrap(this.client.GET("/api/audit", { ...(signal === undefined ? {} : { signal }) }));
  }

  getRuntimeLogs(runtimeInstanceId: string, tail: RuntimeLogParams["tail"] = 200, signal?: AbortSignal): Promise<RuntimeLogs> {
    return this.unwrap(this.client.GET("/api/runtime-instances/{runtime_instance_id}/logs", {
      params: { path: { runtime_instance_id: runtimeInstanceId }, query: { tail } },
      ...(signal === undefined ? {} : { signal }),
    }));
  }

  reloadDefinitions(csrfToken: string): Promise<ManifestReloadResponse> {
    return this.unwrap(this.client.POST("/api/admin/reload", {
      headers: { "X-CSRF-Token": csrfToken },
    }));
  }
}

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL;
export const workspaceApi = new WorkspaceApi({ baseUrl: configuredBaseUrl ?? "" });
