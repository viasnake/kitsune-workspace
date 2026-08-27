import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { workspaceApi } from "../../src/api/generated";
import { AgentsPage } from "../../src/pages/agents-page";
import { agentSummary, viewerSession } from "./fixtures";
import { renderRoute } from "./test-utils";

describe("AgentsPage", () => {
  it("shows the operational columns for registered Agent Definitions", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(viewerSession);
    vi.spyOn(workspaceApi, "listAgents").mockResolvedValue([agentSummary]);

    renderRoute({ element: <AgentsPage />, path: "/agents", entry: "/agents" });

    expect(await screen.findByText("SRE Agent")).toBeInTheDocument();
    expect(screen.getByText("Process")).toBeInTheDocument();
    expect(screen.getByText("常駐")).toBeInTheDocument();
    expect(screen.getAllByText("稼働中")).not.toHaveLength(0);
    expect(screen.getByRole("columnheader", { name: "Heartbeat" })).toBeInTheDocument();
  });

  it("renders a recoverable API error", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(viewerSession);
    vi.spyOn(workspaceApi, "listAgents").mockRejectedValue(new Error("database unavailable"));

    renderRoute({ element: <AgentsPage />, path: "/agents", entry: "/agents" });

    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
    expect(screen.getByRole("button", { name: "再試行" })).toBeEnabled();
  });
});
