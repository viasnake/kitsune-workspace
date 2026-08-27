import { describe, expect, it, vi } from "vitest";
import { ApiError, isApiErrorDocument, WorkspaceApi } from "../../src/api/generated";
import { agentDetail, runningRun } from "./fixtures";

const requestFromCall = (call: [input: RequestInfo | URL, init?: RequestInit | undefined] | undefined): Request => {
  if (call === undefined) throw new Error("fetch was not called");
  return call[0] instanceof Request ? call[0] : new Request(call[0], call[1]);
};

describe("WorkspaceApi generated transport", () => {
  it("accepts typed problem documents and rejects malformed error bodies", () => {
    expect(isApiErrorDocument({ title: "Conflict", status: 409, detail: "Run is already terminal" })).toBe(true);
    expect(isApiErrorDocument({ title: "Invalid", status: "409", detail: ["not", "a", "string"] })).toBe(false);
  });

  it("serializes generated path/query parameters and includes CSRF headers", async () => {
    const fetchMock = vi
      .fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(new Response(JSON.stringify(agentDetail), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify([runningRun]), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(runningRun), { status: 200, headers: { "Content-Type": "application/json" } }));
    const api = new WorkspaceApi({ baseUrl: "https://workspace.example.invalid", fetch: fetchMock });

    await api.getAgent("sre agent/東京");
    await api.listRuns({ agent_id: "sre agent/東京", status: "running", limit: 25 });
    await api.cancelRun("run/001", "csrf-token");

    const agentRequest = requestFromCall(fetchMock.mock.calls[0]);
    expect(new URL(agentRequest.url).pathname).toBe("/api/agents/sre%20agent%2F%E6%9D%B1%E4%BA%AC");

    const runsRequest = requestFromCall(fetchMock.mock.calls[1]);
    expect(Object.fromEntries(new URL(runsRequest.url).searchParams)).toEqual({
      agent_id: "sre agent/東京",
      status: "running",
      limit: "25",
    });

    const cancelRequest = requestFromCall(fetchMock.mock.calls[2]);
    expect(cancelRequest.method).toBe("POST");
    expect(cancelRequest.headers.get("X-CSRF-Token")).toBe("csrf-token");
  });

  it("preserves problem detail and request identity for API failures", async () => {
    const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>().mockResolvedValue(
      new Response(JSON.stringify({ title: "Service Unavailable", detail: "registry lock is unavailable" }), {
        status: 503,
        headers: { "Content-Type": "application/json", "X-Request-ID": "request-17" },
      }),
    );
    const api = new WorkspaceApi({ baseUrl: "https://workspace.example.invalid", fetch: fetchMock });

    const error = await api.listAgents().catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 503, message: "registry lock is unavailable", requestId: "request-17" });
  });
});
