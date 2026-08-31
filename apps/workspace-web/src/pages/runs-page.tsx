import { useMemo, useState } from "react";
import { useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { workspaceApi, type RunStatus } from "../api/generated";
import { queryKeys } from "../query-keys";
import { humanize, isListedValue, RUN_STATUSES } from "../utils";
import { ErrorState, LoadingBlock, PageHeader } from "../components/ui";
import { RunTable } from "../components/run-table";

type RunSearch = { agent?: string; status?: RunStatus };

export function RunsPage() {
  const routeSearch: RunSearch = useSearch({ strict: false });
  const [agentFilter, setAgentFilter] = useState(routeSearch.agent ?? "");
  const [status, setStatus] = useState<RunStatus | "all">(routeSearch.status ?? "all");
  const runsQuery = useQuery({
    queryKey: queryKeys.runs({ agent_id: agentFilter || undefined, status: status === "all" ? undefined : status }),
    queryFn: ({ signal }) =>
      workspaceApi.listRuns(
        {
          ...(agentFilter.trim().length === 0 ? {} : { agent_id: agentFilter.trim() }),
          ...(status === "all" ? {} : { status }),
          limit: 200,
        },
        signal,
      ),
  });
  const runs = useMemo(() => runsQuery.data ?? [], [runsQuery.data]);

  return (
    <div className="page-stack">
      <PageHeader title="Run" description="一回の処理要求を作成から終端状態まで追跡します。" />
      <div className="table-toolbar">
        <label className="search-field">
          <Search aria-hidden="true" size={17} />
          <span className="sr-only">Agent ID で絞り込む</span>
          <input
            type="search"
            value={agentFilter}
            onChange={(event) => setAgentFilter(event.target.value)}
            placeholder="Agent ID で絞り込む"
          />
        </label>
        <label className="select-field compact-select">
          <span>Run Status</span>
          <select
            value={status}
            onChange={(event) => {
              const value = event.target.value;
              if (value === "all" || isListedValue(RUN_STATUSES, value)) setStatus(value);
            }}
          >
            <option value="all">すべて</option>
            {RUN_STATUSES.map(
              (value) => (
                <option value={value} key={value}>
                  {humanize(value)}
                </option>
              ),
            )}
          </select>
        </label>
        <span className="result-count" aria-live="polite">{runs.length} 件</span>
      </div>
      {runsQuery.isLoading ? (
        <LoadingBlock rows={9} label="Run 一覧を読み込み中" />
      ) : runsQuery.error !== null ? (
        <ErrorState error={runsQuery.error} retry={() => void runsQuery.refetch()} />
      ) : (
        <RunTable runs={runs} emptyDescription="Agent 詳細の Manual Invocation、Schedule、Webhook、または Agent 自身から Run が作成されると表示されます。" />
      )}
    </div>
  );
}
