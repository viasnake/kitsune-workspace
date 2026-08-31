import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Bot, CalendarClock, CircleDollarSign, Play, Waypoints } from "lucide-react";
import { workspaceApi } from "../api/generated";
import { queryKeys } from "../query-keys";
import { formatCost, formatDateTime, formatNumber, humanize, shortId } from "../utils";
import { EmptyState, ErrorState, LoadingBlock, PageHeader, SectionHeader, StatusBadge } from "../components/ui";

export function DashboardPage() {
  const dashboardQuery = useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: ({ signal }) => workspaceApi.getDashboard(signal),
    refetchInterval: 30_000,
  });

  if (dashboardQuery.isLoading) {
    return (
      <div className="page-stack">
        <PageHeader title="運用ダッシュボード" description="Agent 群の現在状態、実行、利用量を一つの制御面で確認します。" />
        <LoadingBlock rows={7} label="ダッシュボードを読み込み中" />
      </div>
    );
  }

  if (dashboardQuery.error !== null || dashboardQuery.data === undefined) {
    return (
      <div className="page-stack">
        <PageHeader title="運用ダッシュボード" description="Agent 群の現在状態、実行、利用量を一つの制御面で確認します。" />
        <ErrorState error={dashboardQuery.error} retry={() => void dashboardQuery.refetch()} />
      </div>
    );
  }

  const dashboard = dashboardQuery.data;
  const metrics = [
    { label: "Agent", value: dashboard.agents.total, detail: `${String(dashboard.agents.running)} 稼働中`, icon: Bot },
    { label: "停止", value: dashboard.agents.stopped, detail: "Desired / Actual", icon: Waypoints },
    { label: "Agent 異常", value: dashboard.agents.failed, detail: "failed / lost", icon: AlertTriangle, danger: true },
    { label: "Active Run", value: dashboard.runs.active, detail: "dispatching / running", icon: Play },
    { label: "失敗 Run", value: dashboard.runs.failed, detail: "保存中の履歴", icon: AlertTriangle, danger: dashboard.runs.failed > 0 },
    { label: "Model Request", value: dashboard.usage_today.request_count, detail: "本日", icon: Waypoints },
    { label: "Token", value: dashboard.usage_today.total_tokens, detail: "本日", icon: Waypoints },
    {
      label: "推定費用",
      value: formatCost(dashboard.usage_today.estimated_cost, dashboard.usage_today.currency),
      detail: "取得できた利用量のみ",
      icon: CircleDollarSign,
    },
  ];

  return (
    <div className="page-stack">
      <PageHeader
        title="運用ダッシュボード"
        description="Agent 群の現在状態、実行、利用量を一つの制御面で確認します。"
        actions={
          <Link to="/runs" className="button button-secondary">
            Run を確認
            <ArrowRight aria-hidden="true" size={16} />
          </Link>
        }
      />

      <section className="metric-strip" aria-label="主要指標">
        {metrics.map(({ label, value, detail, icon: Icon, danger = false }) => (
          <div className={danger ? "metric-item metric-danger" : "metric-item"} key={label}>
            <Icon aria-hidden="true" size={17} />
            <div>
              <span>{label}</span>
              <strong>{typeof value === "number" ? formatNumber(value) : value}</strong>
              <small>{detail}</small>
            </div>
          </div>
        ))}
      </section>

      <div className="dashboard-columns">
        <section className="surface-section">
          <SectionHeader
            title="直近の異常"
            description="失敗、Health 低下、配信エラーの新しい順"
            action={
              <Link to="/runs" search={{ status: "failed" }} className="text-link">
                失敗 Run を開く
              </Link>
            }
          />
          {dashboard.recent_anomalies.length === 0 ? (
            <EmptyState title="異常はありません" description="現在、対応が必要な運用イベントはありません。" />
          ) : (
            <ol className="anomaly-list">
              {dashboard.recent_anomalies.map((anomaly) => (
                <li key={anomaly.id}>
                  <StatusBadge status={anomaly.severity} />
                  <div>
                    <strong>{anomaly.message}</strong>
                    <span>
                      {formatDateTime(anomaly.occurred_at)}
                      {anomaly.agent_id !== null && ` · ${anomaly.agent_id}`}
                    </span>
                  </div>
                  {anomaly.run_id !== null && anomaly.run_id !== undefined && (
                    <Link to="/runs/$runId" params={{ runId: anomaly.run_id }} aria-label={`Run ${anomaly.run_id} を開く`}>
                      {shortId(anomaly.run_id)}
                      <ArrowRight aria-hidden="true" size={14} />
                    </Link>
                  )}
                </li>
              ))}
            </ol>
          )}
        </section>

        <section className="surface-section">
          <SectionHeader
            title="次回 Schedule"
            description="有効な定期実行の次回発火"
            action={
              <Link to="/schedules" className="text-link">
                すべて表示
              </Link>
            }
          />
          {dashboard.next_schedules.length === 0 ? (
            <EmptyState title="予定はありません" description="有効な Schedule が登録されると、ここに次回実行が表示されます。" />
          ) : (
            <ol className="schedule-preview-list">
              {dashboard.next_schedules.map((schedule) => (
                <li key={schedule.id}>
                  <CalendarClock aria-hidden="true" size={18} />
                  <div>
                    <Link to="/agents/$agentId" params={{ agentId: schedule.agent_id }}>
                      {schedule.agent_id} / {schedule.handler}
                    </Link>
                    <span>
                      {schedule.cron} · {schedule.timezone} · {humanize(schedule.overlap)}
                    </span>
                  </div>
                  <time dateTime={schedule.next_fire_at}>{formatDateTime(schedule.next_fire_at)}</time>
                </li>
              ))}
            </ol>
          )}
        </section>
      </div>
    </div>
  );
}
