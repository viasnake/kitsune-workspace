import { Link, useParams } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Ban, Clock3, GitBranch, Link2, ScrollText, Timer, Waypoints } from "lucide-react";
import { workspaceApi, type KitsuneEvent, type Run } from "../api/generated";
import { useAuth } from "../auth-context";
import { queryKeys } from "../query-keys";
import { formatDateTime, formatDuration, humanize, isTerminalRunStatus, shortId, stringifyJson } from "../utils";
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

function RunTreeNode({ run, allRuns, currentRunId, ancestors }: { run: Run; allRuns: Run[]; currentRunId: string; ancestors: Set<string> }) {
  if (ancestors.has(run.run_id)) return null;
  const nextAncestors = new Set(ancestors).add(run.run_id);
  const children = allRuns.filter((candidate) => candidate.parent_run_id === run.run_id);
  return (
    <li>
      <Link
        to="/runs/$runId"
        params={{ runId: run.run_id }}
        className={run.run_id === currentRunId ? "run-tree-link run-tree-current" : "run-tree-link"}
        aria-current={run.run_id === currentRunId ? "page" : undefined}
      >
        <span>{run.handler}</span>
        <small>{shortId(run.run_id)}</small>
        <StatusBadge status={run.status} />
      </Link>
      {children.length > 0 && (
        <ul>
          {children.map((child) => <RunTreeNode key={child.run_id} run={child} allRuns={allRuns} currentRunId={currentRunId} ancestors={nextAncestors} />)}
        </ul>
      )}
    </li>
  );
}

function RunTree({ runs, currentRun }: { runs: Run[]; currentRun: Run }) {
  const completeRuns = runs.some((run) => run.run_id === currentRun.run_id) ? runs : [currentRun, ...runs];
  const ids = new Set(completeRuns.map((run) => run.run_id));
  const roots = completeRuns.filter((run) => run.parent_run_id === null || !ids.has(run.parent_run_id));
  return (
    <nav className="run-tree" aria-label="Run Tree">
      <div className="run-tree-heading"><GitBranch aria-hidden="true" size={17} /><strong>Run Tree</strong><span>{completeRuns.length}</span></div>
      <ul>
        {roots.map((root) => <RunTreeNode key={root.run_id} run={root} allRuns={completeRuns} currentRunId={currentRun.run_id} ancestors={new Set()} />)}
      </ul>
    </nav>
  );
}

function EventTimeline({ events }: { events: KitsuneEvent[] }) {
  if (events.length === 0) return <EmptyState title="Event はありません" description="SDK から Event が届くと、この Run の進行が時系列に表示されます。" />;
  const sorted = [...events].sort((left, right) => new Date(left.occurred_at).getTime() - new Date(right.occurred_at).getTime());
  return (
    <ol className="event-timeline">
      {sorted.map((event) => (
        <li key={event.event_id}>
          <div className="timeline-marker" aria-hidden="true" />
          <div className="timeline-content">
            <header>
              <StatusBadge status={event.severity} />
              <strong>{event.type}</strong>
              <time dateTime={event.occurred_at}>{formatDateTime(event.occurred_at)}</time>
            </header>
            <details>
              <summary>Payload</summary>
              <pre>{stringifyJson(event.payload)}</pre>
            </details>
          </div>
        </li>
      ))}
    </ol>
  );
}

export function RunDetailPage() {
  const { runId = "" } = useParams({ strict: false });
  const auth = useAuth();
  const queryClient = useQueryClient();
  const runQuery = useQuery({
    queryKey: queryKeys.run(runId),
    queryFn: ({ signal }) => workspaceApi.getRun(runId, signal),
    refetchInterval: (query) => {
      const run = query.state.data;
      return run === undefined || isTerminalRunStatus(run.status) ? false : 10_000;
    },
  });
  const eventsQuery = useQuery({
    queryKey: queryKeys.runEvents(runId),
    queryFn: ({ signal }) => workspaceApi.getRunEvents(runId, signal),
  });
  const usageQuery = useQuery({
    queryKey: queryKeys.runUsage(runId),
    queryFn: ({ signal }) => workspaceApi.getRunUsage(runId, signal),
  });
  const correlationId = runQuery.data?.correlation_id;
  const treeQuery = useQuery({
    queryKey: ["run-tree", correlationId],
    queryFn: ({ signal }) =>
      correlationId === undefined ? Promise.resolve([]) : workspaceApi.listRuns({ correlation_id: correlationId, limit: 200 }, signal),
    enabled: correlationId !== undefined,
  });
  const cancelMutation = useMutation({
    mutationFn: () => workspaceApi.cancelRun(runId, auth.session?.csrf_token ?? ""),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) }),
        queryClient.invalidateQueries({ queryKey: ["runs"] }),
        queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
      ]);
    },
  });

  if (runQuery.isLoading) return <LoadingBlock rows={11} label="Run 詳細を読み込み中" />;
  if (runQuery.error !== null || runQuery.data === undefined) return <ErrorState error={runQuery.error} retry={() => void runQuery.refetch()} />;

  const run = runQuery.data;
  const cancellable = !isTerminalRunStatus(run.status);

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow={<Link to="/runs" className="breadcrumb-link"><ArrowLeft aria-hidden="true" size={14} />Run 一覧</Link>}
        title={`Run ${shortId(run.run_id, 16)}`}
        description={`${run.agent_id} / ${run.handler} · ${humanize(run.source)}`}
        actions={
          <>
            <StatusBadge status={run.status} pulse={run.status === "running"} />
            {cancellable && auth.can("operator") && (
              <LoadingButton
                type="button"
                className="button button-danger-secondary"
                loading={cancelMutation.isPending}
                onClick={() => cancelMutation.mutate()}
              >
                <Ban aria-hidden="true" size={15} />Cancel
              </LoadingButton>
            )}
            {cancellable && !auth.can("operator") && <PermissionHint>Cancel は operator 以上</PermissionHint>}
          </>
        }
      />
      {cancelMutation.error !== null && <ErrorState error={cancelMutation.error} compact />}

      <div className="run-fact-bar">
        <div><Waypoints aria-hidden="true" size={16} /><span>Correlation ID</span><strong title={run.correlation_id}>{shortId(run.correlation_id, 14)}</strong></div>
        <div><Link2 aria-hidden="true" size={16} /><span>Parent Run</span><strong>{run.parent_run_id === null ? "Root" : <Link to="/runs/$runId" params={{ runId: run.parent_run_id }}>{shortId(run.parent_run_id)}</Link>}</strong></div>
        <div><Clock3 aria-hidden="true" size={16} /><span>Started</span><strong>{formatDateTime(run.started_at)}</strong></div>
        <div><Timer aria-hidden="true" size={16} /><span>Duration</span><strong>{formatDuration(run.started_at, run.ended_at)}</strong></div>
        <div><ScrollText aria-hidden="true" size={16} /><span>Runtime</span><strong title={run.runtime_instance_id ?? undefined}>{shortId(run.runtime_instance_id, 12)}</strong></div>
      </div>

      <div className="run-detail-layout">
        <aside>
          {treeQuery.isLoading ? <LoadingBlock rows={5} label="Run Tree を読み込み中" /> : treeQuery.error !== null ? <ErrorState error={treeQuery.error} compact /> : <RunTree runs={treeQuery.data ?? []} currentRun={run} />}
          <section className="surface-section run-links">
            <SectionHeader title="Observability" />
            <DefinitionList>
              <Definition term="Trace ID"><span className="mono-cell">{run.trace_id ?? "—"}</span></Definition>
              <Definition term="Trace"><ExternalLink href={run.trace_url}>Trace を開く</ExternalLink></Definition>
              <Definition term="Log"><ExternalLink href={run.log_url}>Log を開く</ExternalLink></Definition>
              <Definition term="Deadline">{formatDateTime(run.deadline)}</Definition>
            </DefinitionList>
          </section>
        </aside>

        <div className="run-detail-main">
          <section className="surface-section">
            <SectionHeader title="Event Timeline" description="SSE で進行イベントと状態を更新します。" />
            {eventsQuery.isLoading ? <LoadingBlock rows={6} /> : eventsQuery.error !== null ? <ErrorState error={eventsQuery.error} retry={() => void eventsQuery.refetch()} compact /> : <EventTimeline events={eventsQuery.data ?? []} />}
          </section>

          {run.error !== null && (
            <section className="surface-section run-error-section">
              <SectionHeader title="Error" description="Agent が報告した失敗内容" />
              <JsonBlock value={run.error} label="Run error" />
            </section>
          )}

          <section className="surface-section">
            <SectionHeader title="Input / Output" description="保存方針で無効な場合、Payload は表示されません。" />
            <div className="schema-pair">
              <JsonBlock value={run.input ?? undefined} label="Input" />
              <JsonBlock value={run.output ?? undefined} label="Output" />
            </div>
          </section>

          <section className="surface-section">
            <SectionHeader title="Usage" description="取得できた値のみ。料金は Kitsune が推測しません。" />
            {usageQuery.isLoading ? <LoadingBlock rows={4} /> : usageQuery.error !== null ? <ErrorState error={usageQuery.error} retry={() => void usageQuery.refetch()} compact /> : <UsageTable usage={usageQuery.data ?? run.usage ?? []} />}
          </section>
        </div>
      </div>
    </div>
  );
}
