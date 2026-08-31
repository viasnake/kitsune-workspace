import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { workspaceApi } from "../../src/api/generated";
import { RunDetailPage } from "../../src/pages/run-detail-page";
import { operatorSession, runEvent, runningRun, viewerSession } from "./fixtures";
import { renderRoute } from "./test-utils";

const mockRunQueries = () => {
  vi.spyOn(workspaceApi, "getRun").mockResolvedValue(runningRun);
  vi.spyOn(workspaceApi, "getRunEvents").mockResolvedValue([runEvent]);
  vi.spyOn(workspaceApi, "getRunUsage").mockResolvedValue([]);
  vi.spyOn(workspaceApi, "listRuns").mockResolvedValue([runningRun]);
};

describe("RunDetailPage", () => {
  it("shows lineage, progress events, observability links, and cancels an active Run", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    mockRunQueries();
    const cancelled = { ...runningRun, status: "cancelled" as const, ended_at: "2026-08-24T01:01:00Z" };
    const cancelRun = vi.spyOn(workspaceApi, "cancelRun").mockResolvedValue(cancelled);
    const { user } = renderRoute({ element: <RunDetailPage />, path: "/runs/$runId", entry: `/runs/${runningRun.run_id}` });

    expect(await screen.findByRole("heading", { name: /Run run-00000001/ })).toBeInTheDocument();
    expect(screen.getByText("kitsune.run.progress")).toBeInTheDocument();
    expect(await screen.findByRole("navigation", { name: "Run Tree" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Trace を開く" })).toHaveAttribute("href", runningRun.trace_url);
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(cancelRun).toHaveBeenCalledWith(runningRun.run_id, "csrf-token"));
  });

  it("does not expose Cancel to viewers", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(viewerSession);
    mockRunQueries();
    renderRoute({ element: <RunDetailPage />, path: "/runs/$runId", entry: `/runs/${runningRun.run_id}` });

    expect(await screen.findByRole("heading", { name: /Run run-00000001/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(screen.getByText(/Cancel は operator 以上/)).toBeInTheDocument();
  });
});
