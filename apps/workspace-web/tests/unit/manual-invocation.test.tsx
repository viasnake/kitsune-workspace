import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { workspaceApi, type Handler } from "../../src/api/generated";
import { ManualInvocation } from "../../src/components/manual-invocation";
import { agentDetail, operatorSession, runningRun } from "./fixtures";
import { renderRoute } from "./test-utils";

describe("ManualInvocation", () => {
  it("builds typed fields from JSON Schema and creates a Run", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    const createRun = vi.spyOn(workspaceApi, "createRun").mockResolvedValue(runningRun);
    const { user, router } = renderRoute({
      element: <ManualInvocation agentId="sre-agent" handlers={agentDetail.handlers} csrfToken="csrf-token" allowed />,
      path: "/agents/$agentId",
      entry: "/agents/sre-agent",
      additionalRoutes: [{ path: "/runs/$runId", element: <div>Run destination</div> }],
    });
    await router.load();

    await user.type(screen.getByLabelText(/調査内容/), "database latency");
    await user.click(screen.getByRole("button", { name: "Run を開始" }));

    await waitFor(() => {
      expect(createRun).toHaveBeenCalledWith(
        "sre-agent",
        {
          handler: "investigate",
          input: { message: "database latency", max_results: 10, include_history: false },
          timeout_seconds: 900,
        },
        "csrf-token",
      );
    });
    expect(await screen.findByText("Run destination")).toBeInTheDocument();
  });

  it("uses a JSON editor when no input schema is available and reports parse errors", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    const createRun = vi.spyOn(workspaceApi, "createRun").mockResolvedValue(runningRun);
    const handler: Handler = {
      name: "raw",
      description: null,
      input_schema: null,
      output_schema: null,
      default_timeout_seconds: null,
      max_concurrency: null,
      queue_capacity: null,
      queue_policy: null,
    };
    const { user, router } = renderRoute({
      element: <ManualInvocation agentId="sre-agent" handlers={[handler]} csrfToken="csrf-token" allowed />,
      path: "/agents/$agentId",
      entry: "/agents/sre-agent",
      additionalRoutes: [{ path: "/runs/$runId", element: <div>Run destination</div> }],
    });
    await router.load();

    const editor = screen.getByLabelText("Input JSON");
    await user.clear(editor);
    await user.type(editor, "not-json");
    await user.click(screen.getByRole("button", { name: "Run を開始" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("有効な JSON");
    expect(createRun).not.toHaveBeenCalled();
  });

  it("resolves nested Pydantic model references into generated fields", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    const createRun = vi.spyOn(workspaceApi, "createRun").mockResolvedValue(runningRun);
    const handler: Handler = {
      name: "nested",
      description: null,
      input_schema: {
        type: "object",
        $defs: {
          Target: {
            type: "object",
            required: ["service"],
            properties: { service: { type: "string" } },
          },
        },
        required: ["target"],
        properties: { target: { $ref: "#/$defs/Target" } },
      },
      output_schema: null,
      default_timeout_seconds: 30,
      max_concurrency: null,
      queue_capacity: null,
      queue_policy: null,
    };
    const { user, router } = renderRoute({
      element: <ManualInvocation agentId="sre-agent" handlers={[handler]} csrfToken="csrf-token" allowed />,
      path: "/agents/$agentId",
      entry: "/agents/sre-agent",
      additionalRoutes: [{ path: "/runs/$runId", element: <div>Run destination</div> }],
    });
    await router.load();

    expect(screen.queryByLabelText("Input JSON")).not.toBeInTheDocument();
    const target = screen.getByLabelText(/target/);
    fireEvent.change(target, { target: { value: '{"service":' } });
    expect(target).toHaveValue('{"service":');
    fireEvent.change(target, { target: { value: '{"service":"payments"}' } });
    await user.click(screen.getByRole("button", { name: "Run を開始" }));

    await waitFor(() => {
      expect(createRun).toHaveBeenCalledWith(
        "sre-agent",
        {
          handler: "nested",
          input: { target: { service: "payments" } },
          timeout_seconds: 30,
        },
        "csrf-token",
      );
    });
  });

  it("renders primitive enums as a typed select", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    const createRun = vi.spyOn(workspaceApi, "createRun").mockResolvedValue(runningRun);
    const handler: Handler = {
      name: "prioritize",
      description: null,
      input_schema: {
        type: "object",
        required: ["priority"],
        properties: {
          priority: { type: "string", title: "Priority", enum: ["low", "high"] },
        },
      },
      output_schema: null,
      default_timeout_seconds: null,
      max_concurrency: null,
      queue_capacity: null,
      queue_policy: null,
    };
    const { user, router } = renderRoute({
      element: <ManualInvocation agentId="sre-agent" handlers={[handler]} csrfToken="csrf-token" allowed />,
      path: "/agents/$agentId",
      entry: "/agents/sre-agent",
      additionalRoutes: [{ path: "/runs/$runId", element: <div>Run destination</div> }],
    });
    await router.load();

    await user.selectOptions(screen.getByLabelText(/Priority/), "high");
    await user.click(screen.getByRole("button", { name: "Run を開始" }));

    await waitFor(() => {
      expect(createRun).toHaveBeenCalledWith(
        "sre-agent",
        { handler: "prioritize", input: { priority: "high" } },
        "csrf-token",
      );
    });
  });

  it("renders nullable optional fields expressed with anyOf", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    const createRun = vi.spyOn(workspaceApi, "createRun").mockResolvedValue(runningRun);
    const handler: Handler = {
      name: "optional",
      description: null,
      input_schema: {
        type: "object",
        properties: {
          note: { title: "Note", anyOf: [{ type: "string" }, { type: "null" }] },
        },
      },
      output_schema: null,
      default_timeout_seconds: null,
      max_concurrency: null,
      queue_capacity: null,
      queue_policy: null,
    };
    const { user, router } = renderRoute({
      element: <ManualInvocation agentId="sre-agent" handlers={[handler]} csrfToken="csrf-token" allowed />,
      path: "/agents/$agentId",
      entry: "/agents/sre-agent",
      additionalRoutes: [{ path: "/runs/$runId", element: <div>Run destination</div> }],
    });
    await router.load();

    expect(screen.queryByLabelText("Input JSON")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/Note/), "follow up");
    await user.click(screen.getByRole("button", { name: "Run を開始" }));
    await waitFor(() => {
      expect(createRun).toHaveBeenCalledWith(
        "sre-agent",
        { handler: "optional", input: { note: "follow up" } },
        "csrf-token",
      );
    });
  });

  it("keeps the JSON editor for non-local references", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    const handler: Handler = {
      name: "remote",
      description: null,
      input_schema: {
        type: "object",
        properties: { target: { $ref: "https://schemas.example.invalid/target.json" } },
      },
      output_schema: null,
      default_timeout_seconds: null,
      max_concurrency: null,
      queue_capacity: null,
      queue_policy: null,
    };
    const { router } = renderRoute({
      element: <ManualInvocation agentId="sre-agent" handlers={[handler]} csrfToken="csrf-token" allowed />,
      path: "/agents/$agentId",
      entry: "/agents/sre-agent",
    });
    await router.load();

    expect(screen.getByLabelText("Input JSON")).toBeInTheDocument();
  });

  it("does not expose the execution button to viewers", async () => {
    vi.spyOn(workspaceApi, "getAuthSession").mockResolvedValue(operatorSession);
    const { router } = renderRoute({
      element: <ManualInvocation agentId="sre-agent" handlers={agentDetail.handlers} csrfToken="viewer-csrf" allowed={false} />,
      path: "/agents/$agentId",
      entry: "/agents/sre-agent",
    });
    await router.load();

    expect(screen.queryByRole("button", { name: "Run を開始" })).not.toBeInTheDocument();
    expect(screen.getByText(/operator 権限が必要/)).toBeInTheDocument();
  });
});
