import { useEffect, useState } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import type { StreamEnvelope } from "./api/generated";
import { workspaceApi } from "./api/generated";
import { isJsonObject, parseJsonValue, type JsonValue } from "./json";
import { queryKeys } from "./query-keys";

export type StreamConnectionState = "connecting" | "connected" | "reconnecting";

const stringField = (value: JsonValue, field: string): string | null => {
  if (!isJsonObject(value)) return null;
  const candidate = value[field];
  return typeof candidate === "string" ? candidate : null;
};

export const parseStreamEnvelope = (raw: string, fallbackType?: StreamEnvelope["type"]): StreamEnvelope | null => {
  const parsed = parseJsonValue(raw);
  if (!parsed.ok || !isJsonObject(parsed.value)) return null;
  const type = typeof parsed.value.type === "string" ? parsed.value.type : fallbackType;
  if (type !== "run" && type !== "runtime" && type !== "event" && type !== "reload") return null;
  const data = parsed.value.data;
  if (data === undefined) return null;
  return {
    type,
    data,
    occurred_at: typeof parsed.value.occurred_at === "string" ? parsed.value.occurred_at : new Date().toISOString(),
  };
};

export const applyStreamUpdate = async (queryClient: QueryClient, envelope: StreamEnvelope): Promise<void> => {
  const agentId = stringField(envelope.data, "agent_id");
  const runId = stringField(envelope.data, "run_id");
  const runtimeInstanceId = stringField(envelope.data, "runtime_instance_id");

  await Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
    envelope.type === "runtime" || envelope.type === "reload"
      ? queryClient.invalidateQueries({ queryKey: queryKeys.agents })
      : Promise.resolve(),
    agentId !== null
      ? queryClient.invalidateQueries({ queryKey: queryKeys.agent(agentId) })
      : queryClient.invalidateQueries({ queryKey: ["agent"] }),
    envelope.type === "run" || envelope.type === "event"
      ? queryClient.invalidateQueries({ queryKey: ["runs"] })
      : Promise.resolve(),
    envelope.type === "run" || envelope.type === "event"
      ? queryClient.invalidateQueries({ queryKey: ["run-tree"] })
      : Promise.resolve(),
    runId === null ? Promise.resolve() : queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) }),
    runId === null ? Promise.resolve() : queryClient.invalidateQueries({ queryKey: queryKeys.runEvents(runId) }),
    runId === null ? Promise.resolve() : queryClient.invalidateQueries({ queryKey: queryKeys.runUsage(runId) }),
    runtimeInstanceId === null
      ? Promise.resolve()
      : queryClient.invalidateQueries({ queryKey: queryKeys.runtimeLogs(runtimeInstanceId) }),
    envelope.type === "reload" ? queryClient.invalidateQueries({ queryKey: queryKeys.schedules }) : Promise.resolve(),
  ]);
};

export const resyncWorkspaceQueries = async (queryClient: QueryClient): Promise<void> => {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.session }),
    queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
    queryClient.invalidateQueries({ queryKey: queryKeys.health }),
    queryClient.invalidateQueries({ queryKey: queryKeys.agents }),
    queryClient.invalidateQueries({ queryKey: ["agent"] }),
    queryClient.invalidateQueries({ queryKey: ["runs"] }),
    queryClient.invalidateQueries({ queryKey: ["run"] }),
    queryClient.invalidateQueries({ queryKey: ["run-tree"] }),
    queryClient.invalidateQueries({ queryKey: ["run-events"] }),
    queryClient.invalidateQueries({ queryKey: ["run-usage"] }),
    queryClient.invalidateQueries({ queryKey: queryKeys.schedules }),
    queryClient.invalidateQueries({ queryKey: queryKeys.audit }),
    queryClient.invalidateQueries({ queryKey: ["runtime-logs"] }),
  ]);
};

export const useWorkspaceStream = (): StreamConnectionState => {
  const queryClient = useQueryClient();
  const [state, setState] = useState<StreamConnectionState>("connecting");

  useEffect(() => {
    const source = new EventSource(`${workspaceApi.baseUrl}/api/stream`, { withCredentials: true });
    const handle = (fallbackType?: StreamEnvelope["type"]): EventListener => (event) => {
      if (!(event instanceof MessageEvent)) return;
      const data: unknown = event.data;
      if (typeof data !== "string") return;
      const envelope = parseStreamEnvelope(data, fallbackType);
      if (envelope !== null) void applyStreamUpdate(queryClient, envelope);
    };
    const listeners: Record<string, EventListener> = {
      run: handle("run"),
      runtime: handle("runtime"),
      event: handle("event"),
      reload: handle("reload"),
    };
    const messageListener = handle();
    source.onopen = () => {
      setState("connected");
      void resyncWorkspaceQueries(queryClient);
    };
    source.onerror = () => setState("reconnecting");
    source.addEventListener("message", messageListener);
    for (const [name, listener] of Object.entries(listeners)) source.addEventListener(name, listener);

    return () => {
      source.removeEventListener("message", messageListener);
      for (const [name, listener] of Object.entries(listeners)) source.removeEventListener(name, listener);
      source.close();
    };
  }, [queryClient]);

  return state;
};
