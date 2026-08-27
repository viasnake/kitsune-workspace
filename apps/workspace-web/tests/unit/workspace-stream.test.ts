import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { queryKeys } from "../../src/query-keys";
import {
  applyStreamUpdate,
  parseStreamEnvelope,
  resyncWorkspaceQueries,
} from "../../src/use-workspace-stream";

describe("Workspace stream updates", () => {
  it("invalidates one Run timeline and Agent from an ingested Event envelope", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue();
    const envelope = parseStreamEnvelope(
      JSON.stringify({
        type: "event",
        data: {
          event_id: "event-1",
          type: "kitsune.run.succeeded",
          agent_id: "demo-agent",
          run_id: "run-1",
        },
        occurred_at: "2026-08-24T01:00:00Z",
      }),
    );

    expect(envelope).not.toBeNull();
    if (envelope === null) throw new Error("expected a valid stream envelope");
    await applyStreamUpdate(client, envelope);

    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.agent("demo-agent") });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.run("run-1") });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.runEvents("run-1") });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.runUsage("run-1") });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["run-tree"] });
  });

  it("refreshes active Agent detail and logs for a runtime update without agent_id", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue();

    await applyStreamUpdate(client, {
      type: "runtime",
      data: { runtime_instance_id: "runtime-1", status: "failed" },
      occurred_at: "2026-08-24T01:00:00Z",
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.runtimeLogs("runtime-1") });
  });

  it("resynchronizes persisted views whenever the best-effort stream reconnects", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue();

    await resyncWorkspaceQueries(client);

    for (const queryKey of [
      queryKeys.dashboard,
      queryKeys.agents,
      ["agent"],
      ["runs"],
      ["run"],
      ["run-events"],
      ["run-usage"],
      queryKeys.schedules,
    ]) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey });
    }
  });
});
