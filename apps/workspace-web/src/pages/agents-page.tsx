import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, FileCode2, Search } from "lucide-react";
import { workspaceApi, type RuntimeStatus } from "../api/generated";
import { queryKeys } from "../query-keys";
import { formatDateTime, formatRelativeTime, humanize, isListedValue } from "../utils";
import { EmptyState, ErrorState, LoadingBlock, PageHeader, StatusBadge } from "../components/ui";

const AGENT_STATUS_FILTERS = ["ready", "starting", "stopped", "unhealthy", "failed", "lost"] satisfies readonly RuntimeStatus[];

export function AgentsPage() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<RuntimeStatus | "all">("all");
  const agentsQuery = useQuery({
    queryKey: queryKeys.agents,
    queryFn: ({ signal }) => workspaceApi.listAgents(signal),
  });

  const filteredAgents = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return (agentsQuery.data ?? []).filter((agent) => {
      const matchesQuery =
        query.length === 0 ||
        agent.agent_id.toLocaleLowerCase().includes(query) ||
        agent.display_name.toLocaleLowerCase().includes(query) ||
        (agent.description?.toLocaleLowerCase().includes(query) ?? false);
      return matchesQuery && (status === "all" || agent.actual_state === status);
    });
  }, [agentsQuery.data, search, status]);

  return (
    <div className="page-stack">
      <PageHeader
        title="Agent"
        description="Agent Definition と、実際に報告された Runtime の状態を並べて確認します。"
        actions={
          <span className="source-of-truth-note">
            <FileCode2 aria-hidden="true" size={16} />
            Definition は Manifest で管理
          </span>
        }
      />

      <div className="table-toolbar">
        <label className="search-field">
          <Search aria-hidden="true" size={17} />
          <span className="sr-only">Agent を検索</span>
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="ID、表示名、説明で検索"
          />
        </label>
        <label className="select-field compact-select">
          <span>Actual State</span>
          <select
            value={status}
            onChange={(event) => {
              const value = event.target.value;
              if (value === "all" || isListedValue(AGENT_STATUS_FILTERS, value)) setStatus(value);
            }}
          >
            <option value="all">すべて</option>
            {AGENT_STATUS_FILTERS.map((value) => (
              <option key={value} value={value}>
                {humanize(value)}
              </option>
            ))}
          </select>
        </label>
        <span className="result-count" aria-live="polite">
          {filteredAgents.length} 件
        </span>
      </div>

      {agentsQuery.isLoading ? (
        <LoadingBlock rows={8} label="Agent 一覧を読み込み中" />
      ) : agentsQuery.error !== null ? (
        <ErrorState error={agentsQuery.error} retry={() => void agentsQuery.refetch()} />
      ) : filteredAgents.length === 0 ? (
        <EmptyState
          title={agentsQuery.data?.length === 0 ? "Agent Definition がありません" : "条件に一致する Agent がありません"}
          description={
            agentsQuery.data?.length === 0
              ? "設定ディレクトリへ Manifest を追加し、管理者が再読み込みすると表示されます。"
              : "検索語または Actual State の絞り込みを変更してください。"
          }
        />
      ) : (
        <div className="table-frame">
          <table className="data-table agent-table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Version</th>
                <th>Runtime</th>
                <th>Desired</th>
                <th>Actual</th>
                <th>Heartbeat</th>
                <th>Active Run</th>
                <th>最終実行</th>
                <th aria-label="詳細" />
              </tr>
            </thead>
            <tbody>
              {filteredAgents.map((agent) => (
                <tr key={agent.agent_id}>
                  <td data-label="Agent">
                    <Link className="table-primary-link" to="/agents/$agentId" params={{ agentId: agent.agent_id }}>
                      <strong>{agent.display_name}</strong>
                      <span>{agent.agent_id}</span>
                    </Link>
                  </td>
                  <td data-label="Version" className="mono-cell">
                    {agent.version ?? "—"}
                  </td>
                  <td data-label="Runtime">
                    <span className="stacked-cell">
                      <strong>{humanize(agent.runtime_adapter)}</strong>
                      <span>{humanize(agent.runtime_mode)}</span>
                    </span>
                  </td>
                  <td data-label="Desired">{humanize(agent.desired_state)}</td>
                  <td data-label="Actual">
                    <StatusBadge status={agent.actual_state} pulse={agent.actual_state === "ready"} />
                  </td>
                  <td data-label="Heartbeat" title={formatDateTime(agent.last_heartbeat)}>
                    {formatRelativeTime(agent.last_heartbeat)}
                  </td>
                  <td data-label="Active Run" className="numeric-cell">
                    {agent.active_runs}
                  </td>
                  <td data-label="最終実行">
                    {agent.last_run === null ? (
                      "—"
                    ) : (
                      <span className="stacked-cell">
                        <StatusBadge status={agent.last_run.status} />
                        <span>{formatRelativeTime(agent.last_run.created_at)}</span>
                      </span>
                    )}
                  </td>
                  <td className="row-action">
                    <Link to="/agents/$agentId" params={{ agentId: agent.agent_id }} aria-label={`${agent.display_name} の詳細`}>
                      <ArrowRight aria-hidden="true" size={17} />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
