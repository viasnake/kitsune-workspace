import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { workspaceApi } from "../../src/api/generated";
import { AgentDetailPage } from "../../src/pages/agent-detail-page";
import { agentDetail, operatorSession, runningRun, viewerSession } from "./fixtures";
import { renderRoute } from "./test-utils";

const mockAgentQueries = () => {
  vi.spyOn(workspaceApi, "getAgent").mockResolvedValue(agentDetail);
  vi.spyOn(workspaceApi, "listRuns").mockResolvedValue([runningRun]);
};

describe("AgentDetailPage role controls", () => {
  it("shows health and manual invocation but hides lifecycle buttons for viewers", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(viewerSession);
    mockAgentQueries();
    renderRoute({ element: <AgentDetailPage />, path: "/agents/$agentId", entry: "/agents/sre-agent" });

    expect(await screen.findByRole("heading", { name: "SRE Agent" })).toBeInTheDocument();
    expect(screen.getAllByText("正常")).not.toHaveLength(0);
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restart" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run を開始" })).not.toBeInTheDocument();
    expect(screen.getByText(/Runtime 操作は operator 以上/)).toBeInTheDocument();
  });

  it("shows lifecycle and manual invocation actions to operators", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    mockAgentQueries();
    renderRoute({ element: <AgentDetailPage />, path: "/agents/$agentId", entry: "/agents/sre-agent" });

    expect(await screen.findByRole("button", { name: "Stop" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Restart" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Run を開始" })).toBeEnabled();
  });

  it("does not offer resident lifecycle actions for an ephemeral Agent", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    vi.spyOn(workspaceApi, "getAgent").mockResolvedValue({
      ...agentDetail,
      runtime_mode: "ephemeral",
      actual_state: "stopped",
    });
    vi.spyOn(workspaceApi, "listRuns").mockResolvedValue([]);
    renderRoute({ element: <AgentDetailPage />, path: "/agents/$agentId", entry: "/agents/sre-agent" });

    expect(await screen.findByText("一時起動 Runtime は Run ごとに管理")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restart" })).not.toBeInTheDocument();
  });
});
