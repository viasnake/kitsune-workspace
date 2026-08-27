import { useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Box,
  ListChecks,
  Play,
  Plug,
  RefreshCw,
  RotateCw,
  Square,
  TerminalSquare,
} from "lucide-react";
import { workspaceApi, type RuntimeInstance } from "../api/generated";
import { useAuth } from "../auth-context";
import { queryKeys } from "../query-keys";
import {
  formatDateTime,
  formatRelativeTime,
  humanize,
  shortId,
} from "../utils";
import { ManualInvocation } from "../components/manual-invocation";
import { RunTable } from "../components/run-table";
import { UsageTable } from "../components/usage-table";
import {
  Definition,
  DefinitionList,
  EmptyState,
  ErrorState,
  ExternalLink,
  JsonBlock,
  LoadingBlock,
  LoadingButton,
  PageHeader,
  PermissionHint,
  SectionHeader,
  StatusBadge,
} from "../components/ui";

type AgentTab = "overview" | "configuration" | "capabilities" | "runtime" | "automation" | "activity";

const tabs: { id: AgentTab; label: string }[] = [
  { id: "overview", label: "概要 / 実行" },
  { id: "configuration", label: "Definition / Descriptor" },
  { id: "capabilities", label: "Handler / Plugin" },
  { id: "runtime", label: "Runtime Instance" },
  { id: "automation", label: "Trigger / Schedule" },
  { id: "activity", label: "履歴 / Usage" },
];

function RuntimeLogPanel({ runtime }: { runtime: RuntimeInstance }) {
  const [open, setOpen] = useState(false);
  const logsQuery = useQuery({
    queryKey: queryKeys.runtimeLogs(runtime.runtime_instance_id),
    queryFn: ({ signal }) => workspaceApi.getRuntimeLogs(runtime.runtime_instance_id, 200, signal),
    enabled: open && runtime.adapter !== "external",
  });

  if (runtime.adapter === "external") {
    return <ExternalLink href={runtime.log_url}>外部ログを開く</ExternalLink>;
  }

  return (
    <div className="runtime-log-control">
      <button type="button" className="button button-secondary button-small" onClick={() => setOpen((current) => !current)} aria-expanded={open}>
        <TerminalSquare aria-hidden="true" size={15} />
        {open ? "ログを閉じる" : "直近ログ"}
      </button>
      {open && (
        <div className="log-tail">
          <div className="log-tail-toolbar">
            <span>{logsQuery.data?.source ?? runtime.adapter} · tail 200</span>
            <button type="button" className="button button-quiet button-small" onClick={() => void logsQuery.refetch()} disabled={logsQuery.isFetching}>
              <RefreshCw className={logsQuery.isFetching ? "spin" : undefined} aria-hidden="true" size={14} />
              更新
            </button>
          </div>
          {logsQuery.isLoading ? (
            <LoadingBlock rows={4} label="Runtime ログを読み込み中" />
          ) : logsQuery.error !== null ? (
            <ErrorState error={logsQuery.error} retry={() => void logsQuery.refetch()} compact />
          ) : logsQuery.data?.lines.length === 0 ? (
            <p className="log-empty">出力されたログはありません。</p>
          ) : (
            <pre tabIndex={0}>{logsQuery.data?.lines.join("\n")}</pre>
          )}
          {logsQuery.data?.truncated === true && <small>古い行は省略されています。</small>}
        </div>
      )}
    </div>
  );
}

export function AgentDetailPage() {
  const { agentId = "" } = useParams({ strict: false });
  const [activeTab, setActiveTab] = useState<AgentTab>("overview");
  const auth = useAuth();
  const queryClient = useQueryClient();
  const agentQuery = useQuery({
    queryKey: queryKeys.agent(agentId),
    queryFn: ({ signal }) => workspaceApi.getAgent(agentId, signal),
  });
  const runsQuery = useQuery({
    queryKey: queryKeys.runs({ agent_id: agentId }),
    queryFn: ({ signal }) => workspaceApi.listRuns({ agent_id: agentId, limit: 50 }, signal),
  });
  const lifecycleMutation = useMutation({
    mutationFn: (action: "start" | "stop" | "restart") => {
      const csrfToken = auth.session?.csrf_token ?? "";
      if (action === "start") return workspaceApi.startAgent(agentId, csrfToken);
      if (action === "stop") return workspaceApi.stopAgent(agentId, csrfToken);
      return workspaceApi.restartAgent(agentId, csrfToken);
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.agent(agentId) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.agents }),
        queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
      ]);
    },
  });

  if (agentQuery.isLoading) {
    return <LoadingBlock rows={10} label="Agent 詳細を読み込み中" />;
  }
  if (agentQuery.error !== null || agentQuery.data === undefined) {
    return <ErrorState error={agentQuery.error} retry={() => void agentQuery.refetch()} />;
  }

  const agent = agentQuery.data;
  const isExternal = agent.runtime_adapter === "external";
  const hasManagedLifecycle = agent.runtime_mode === "resident" && !isExternal;
  const canOperate = auth.can("operator");
  const running = agent.actual_state === "ready" || agent.actual_state === "starting" || agent.actual_state === "unhealthy";

  const requestStop = () => {
    if (window.confirm(`${agent.display_name} を停止します。実行中の Run に影響する可能性があります。続行しますか？`)) {
      lifecycleMutation.mutate("stop");
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow={
          <Link to="/agents" className="breadcrumb-link">
            <ArrowLeft aria-hidden="true" size={14} />
            Agent 一覧
          </Link>
        }
        title={agent.display_name}
        description={`${agent.agent_id} · ${agent.description ?? "説明なし"}`}
        actions={
          <>
            <StatusBadge status={agent.actual_state} pulse={agent.actual_state === "ready"} />
            {isExternal ? (
              <span className="source-of-truth-note">外部 Runtime が管理</span>
            ) : agent.runtime_mode === "ephemeral" ? (
              <span className="source-of-truth-note">一時起動 Runtime は Run ごとに管理</span>
            ) : canOperate && hasManagedLifecycle ? (
              <div className="button-group" aria-label="Runtime 操作">
                {!running && (
                  <LoadingButton
                    className="button button-primary"
                    type="button"
                    loading={lifecycleMutation.isPending && lifecycleMutation.variables === "start"}
                    disabled={lifecycleMutation.isPending}
                    onClick={() => lifecycleMutation.mutate("start")}
                  >
                    <Play aria-hidden="true" size={15} />
                    Start
                  </LoadingButton>
                )}
                {running && (
                  <LoadingButton
                    className="button button-danger-secondary"
                    type="button"
                    loading={lifecycleMutation.isPending && lifecycleMutation.variables === "stop"}
                    disabled={lifecycleMutation.isPending}
                    onClick={requestStop}
                  >
                    <Square aria-hidden="true" size={14} />
                    Stop
                  </LoadingButton>
                )}
                <LoadingButton
                  className="button button-secondary"
                  type="button"
                  loading={lifecycleMutation.isPending && lifecycleMutation.variables === "restart"}
                  disabled={!running || lifecycleMutation.isPending}
                  onClick={() => lifecycleMutation.mutate("restart")}
                >
                  <RotateCw aria-hidden="true" size={15} />
                  Restart
                </LoadingButton>
              </div>
            ) : (
              <PermissionHint>Runtime 操作は operator 以上</PermissionHint>
            )}
          </>
        }
      />

      {lifecycleMutation.error !== null && <ErrorState error={lifecycleMutation.error} compact />}

      <div className="agent-fact-bar">
        <div><span>Version</span><strong>{agent.version ?? "未報告"}</strong></div>
        <div><span>Build</span><strong title={agent.descriptor?.build_revision ?? undefined}>{shortId(agent.descriptor?.build_revision, 12)}</strong></div>
        <div><span>Runtime</span><strong>{humanize(agent.runtime_adapter)} / {humanize(agent.runtime_mode)}</strong></div>
        <div><span>Desired</span><strong>{humanize(agent.desired_state)}</strong></div>
        <div><span>Heartbeat</span><strong title={formatDateTime(agent.last_heartbeat)}>{formatRelativeTime(agent.last_heartbeat)}</strong></div>
        <div><span>Active Run</span><strong>{agent.active_runs}</strong></div>
      </div>

      <div className="tabs" role="tablist" aria-label="Agent 詳細">
        {tabs.map((tab) => (
          <button
            type="button"
            role="tab"
            id={`tab-${tab.id}`}
            aria-controls={`panel-${tab.id}`}
            aria-selected={activeTab === tab.id}
            tabIndex={activeTab === tab.id ? 0 : -1}
            className={activeTab === tab.id ? "tab tab-active" : "tab"}
            onClick={() => setActiveTab(tab.id)}
            onKeyDown={(event) => {
              const currentIndex = tabs.findIndex((item) => item.id === tab.id);
              let nextIndex: number | null = null;
              if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
              if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
              if (event.key === "Home") nextIndex = 0;
              if (event.key === "End") nextIndex = tabs.length - 1;
              if (nextIndex === null) return;
              event.preventDefault();
              const nextTab = tabs[nextIndex];
              if (nextTab === undefined) return;
              setActiveTab(nextTab.id);
              requestAnimationFrame(() => document.getElementById(`tab-${nextTab.id}`)?.focus());
            }}
            key={tab.id}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <section id={`panel-${activeTab}`} role="tabpanel" aria-labelledby={`tab-${activeTab}`} className="tab-panel">
        {activeTab === "overview" && (
          <div className="overview-layout">
            <div className="surface-section health-section">
              <SectionHeader title="Health" description="Descriptor と Runtime heartbeat を集約した状態" />
              <div className="health-summary">
                <StatusBadge status={agent.health.status} pulse={agent.health.status === "healthy"} />
                <span>Heartbeat age: {agent.health.heartbeat_age_seconds === null ? "未取得" : `${String(agent.health.heartbeat_age_seconds)} 秒`}</span>
              </div>
              {agent.health.checks.length === 0 ? (
                <p className="text-muted">個別 Health check は報告されていません。</p>
              ) : (
                <ul className="health-check-list">
                  {agent.health.checks.map((check) => (
                    <li key={check.name}>
                      <StatusBadge status={check.status} />
                      <strong>{check.name}</strong>
                      <span>{check.detail ?? "詳細なし"}</span>
                    </li>
                  ))}
                </ul>
              )}
              <DefinitionList>
                <Definition term="Trace"><ExternalLink href={agent.trace_url}>Trace を開く</ExternalLink></Definition>
                <Definition term="Log"><ExternalLink href={agent.log_url}>外部ログを開く</ExternalLink></Definition>
              </DefinitionList>
            </div>
            <ManualInvocation agentId={agent.agent_id} handlers={agent.handlers} csrfToken={auth.session?.csrf_token ?? ""} allowed={canOperate} />
          </div>
        )}

        {activeTab === "configuration" && (
          <div className="split-json-layout">
            <div>
              <SectionHeader title="Agent Definition" description="Manifest から読み込んだ desired configuration の snapshot" />
              <JsonBlock value={agent.manifest} label="Manifest snapshot" />
            </div>
            <div>
              <SectionHeader title="Reported Descriptor" description="起動した SDK が実際に報告した capability と build" />
              {agent.descriptor === null ? (
                <EmptyState title="Descriptor がありません" description="Runtime が起動して register すると、報告内容が表示されます。" />
              ) : (
                <JsonBlock value={agent.descriptor} label="Descriptor" />
              )}
            </div>
          </div>
        )}

        {activeTab === "capabilities" && (
          <div className="page-stack compact-stack">
            <section className="surface-section">
              <SectionHeader title="Handler" description="Workspace または Scheduler から呼び出せる型付き entry point" />
              {agent.handlers.length === 0 ? (
                <EmptyState title="Handler がありません" description="起動中の SDK から Handler が報告されていません。" />
              ) : (
                <div className="handler-list">
                  {agent.handlers.map((handler) => (
                    <details key={handler.name}>
                      <summary>
                        <span><ListChecks aria-hidden="true" size={17} /><strong>{handler.name}</strong></span>
                        <small>{handler.default_timeout_seconds === null ? "timeout 未設定" : `${String(handler.default_timeout_seconds)} 秒`}</small>
                      </summary>
                      <p>{handler.description ?? "説明なし"}</p>
                      <div className="schema-pair">
                        <JsonBlock value={handler.input_schema} label="Input Schema" />
                        <JsonBlock value={handler.output_schema} label="Output Schema" />
                      </div>
                    </details>
                  ))}
                </div>
              )}
            </section>
            <section className="surface-section">
              <SectionHeader title="Plugin" description="Application 起動時に確定した横断機能" />
              {agent.plugins.length === 0 ? (
                <EmptyState title="Plugin はありません" description="この Agent Application は Plugin を報告していません。" />
              ) : (
                <ul className="plugin-list">
                  {agent.plugins.map((plugin) => (
                    <li key={plugin.name}>
                      <Plug aria-hidden="true" size={18} />
                      <div><strong>{plugin.name}</strong><span>{plugin.version ?? "version 未報告"}</span></div>
                      {plugin.critical && <span className="tag">critical</span>}
                      <span className="tag">{plugin.status}</span>
                      {plugin.error !== null && plugin.error !== undefined && <p>{plugin.error}</p>}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}

        {activeTab === "runtime" && (
          <section className="surface-section">
            <SectionHeader title="Runtime Instance" description="一つのプロセス、コンテナ、または外部 endpoint を一個体として追跡します。" />
            {agent.runtime_instances.length === 0 ? (
              <EmptyState title="Runtime Instance がありません" description="Desired State が running の場合、起動処理と失敗内容を確認してください。" />
            ) : (
              <div className="runtime-instance-list">
                {agent.runtime_instances.map((runtime) => (
                  <article key={runtime.runtime_instance_id} className="runtime-instance">
                    <header>
                      <div>
                        <Box aria-hidden="true" size={19} />
                        <strong>{shortId(runtime.runtime_instance_id, 14)}</strong>
                        <StatusBadge status={runtime.status} pulse={runtime.status === "ready"} />
                      </div>
                      <RuntimeLogPanel runtime={runtime} />
                    </header>
                    <DefinitionList>
                      <Definition term="Adapter / Mode">{humanize(runtime.adapter)} / {humanize(runtime.mode)}</Definition>
                      <Definition term="PID / Container">{runtime.pid ?? runtime.container_id ?? "—"}</Definition>
                      <Definition term="Endpoint"><ExternalLink href={runtime.endpoint}>{runtime.endpoint ?? "未設定"}</ExternalLink></Definition>
                      <Definition term="Started">{formatDateTime(runtime.started_at)}</Definition>
                      <Definition term="Heartbeat">{formatDateTime(runtime.last_heartbeat_at)}</Definition>
                      <Definition term="Restart attempt">{runtime.restart_attempts}</Definition>
                      <Definition term="Last exit">{runtime.last_exit_code ?? "—"}</Definition>
                      <Definition term="Trace"><ExternalLink href={runtime.trace_url}>Trace を開く</ExternalLink></Definition>
                    </DefinitionList>
                    {runtime.last_error !== null && <div className="inline-runtime-error">{runtime.last_error}</div>}
                  </article>
                ))}
              </div>
            )}
          </section>
        )}

        {activeTab === "automation" && (
          <div className="page-stack compact-stack">
            <section className="surface-section">
              <SectionHeader title="Trigger" description="Run を作成する直接の契機" />
              {agent.triggers.length === 0 ? (
                <EmptyState title="Trigger はありません" description="Manifest に Trigger を宣言すると表示されます。" />
              ) : (
                <div className="table-frame">
                  <table className="data-table">
                    <thead><tr><th>ID</th><th>Type</th><th>Handler</th><th>Schedule</th><th>状態</th></tr></thead>
                    <tbody>
                      {agent.triggers.map((trigger) => (
                        <tr key={trigger.trigger_id}>
                          <td data-label="ID" className="mono-cell">{trigger.trigger_id}</td>
                          <td data-label="Type">{humanize(trigger.type)}</td>
                          <td data-label="Handler">{trigger.handler}</td>
                          <td data-label="Schedule">{trigger.cron === null ? "—" : `${trigger.cron} · ${trigger.timezone ?? "UTC"}`}</td>
                          <td data-label="状態"><StatusBadge status={trigger.enabled ? "enabled" : "disabled"} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
            <section className="surface-section">
              <SectionHeader title="Schedule" description="次回発火と重複ポリシー" />
              {agent.schedules.length === 0 ? (
                <EmptyState title="Schedule はありません" description="type: schedule の Trigger が登録されていません。" />
              ) : (
                <div className="table-frame">
                  <table className="data-table">
                    <thead><tr><th>Handler</th><th>Cron / Timezone</th><th>Overlap</th><th>次回</th><th>最終結果</th></tr></thead>
                    <tbody>
                      {agent.schedules.map((schedule) => (
                        <tr key={schedule.id}>
                          <td data-label="Handler">{schedule.handler}</td>
                          <td data-label="Cron / Timezone" className="mono-cell">{schedule.cron} · {schedule.timezone}</td>
                          <td data-label="Overlap">{humanize(schedule.overlap)}</td>
                          <td data-label="次回">{formatDateTime(schedule.next_fire_at)}</td>
                          <td data-label="最終結果">{schedule.last_outcome === null ? "—" : <StatusBadge status={schedule.last_outcome} />}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        )}

        {activeTab === "activity" && (
          <div className="page-stack compact-stack">
            <section className="surface-section">
              <SectionHeader title="Run 履歴" description="新しい順に 50 件" action={<Link to="/runs" search={{ agent: agent.agent_id }} className="text-link">すべての Run</Link>} />
              {runsQuery.isLoading ? <LoadingBlock rows={5} /> : runsQuery.error !== null ? <ErrorState error={runsQuery.error} compact /> : <RunTable runs={runsQuery.data ?? []} />}
            </section>
            <section className="surface-section">
              <SectionHeader title="Usage" description="取得できた Provider / Model 利用量のみ" />
              <UsageTable usage={agent.usage} />
            </section>
          </div>
        )}
      </section>
    </div>
  );
}
