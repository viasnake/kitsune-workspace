import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { CalendarClock } from "lucide-react";
import { workspaceApi } from "../api/generated";
import { queryKeys } from "../query-keys";
import { formatDateTime, humanize } from "../utils";
import { EmptyState, ErrorState, LoadingBlock, PageHeader, StatusBadge } from "../components/ui";

export function SchedulesPage() {
  const schedulesQuery = useQuery({
    queryKey: queryKeys.schedules,
    queryFn: ({ signal }) => workspaceApi.listSchedules(signal),
  });

  return (
    <div className="page-stack">
      <PageHeader title="Schedule" description="Manifest で宣言された定期実行と、次回発火を確認します。" />
      {schedulesQuery.isLoading ? (
        <LoadingBlock rows={7} label="Schedule を読み込み中" />
      ) : schedulesQuery.error !== null ? (
        <ErrorState error={schedulesQuery.error} retry={() => void schedulesQuery.refetch()} />
      ) : schedulesQuery.data?.length === 0 ? (
        <EmptyState
          title="Schedule はありません"
          description="Agent Manifest に type: schedule の Trigger を追加し、Definition を再読み込みすると表示されます。"
        />
      ) : (
        <div className="table-frame">
          <table className="data-table schedule-table">
            <thead>
              <tr>
                <th>Agent / Handler</th>
                <th>Cron</th>
                <th>Timezone</th>
                <th>Overlap</th>
                <th>次回発火</th>
                <th>最終発火</th>
                <th>最終結果</th>
                <th>状態</th>
              </tr>
            </thead>
            <tbody>
              {schedulesQuery.data?.map((schedule) => (
                <tr key={schedule.id}>
                  <td data-label="Agent / Handler">
                    <span className="stacked-cell">
                      <Link to="/agents/$agentId" params={{ agentId: schedule.agent_id }}><strong>{schedule.agent_id}</strong></Link>
                      <span>{schedule.handler}</span>
                    </span>
                  </td>
                  <td data-label="Cron" className="mono-cell">{schedule.cron}</td>
                  <td data-label="Timezone">{schedule.timezone}</td>
                  <td data-label="Overlap">{humanize(schedule.overlap)}</td>
                  <td data-label="次回発火">
                    <span className="schedule-time"><CalendarClock aria-hidden="true" size={15} />{formatDateTime(schedule.next_fire_at)}</span>
                  </td>
                  <td data-label="最終発火">{formatDateTime(schedule.last_fire_at)}</td>
                  <td data-label="最終結果">{schedule.last_outcome === null ? "—" : <StatusBadge status={schedule.last_outcome} />}</td>
                  <td data-label="状態"><StatusBadge status={schedule.enabled ? "enabled" : "disabled"} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
