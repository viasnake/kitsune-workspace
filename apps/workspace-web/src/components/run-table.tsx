import { Link } from "@tanstack/react-router";
import { ArrowRight } from "lucide-react";
import type { Run } from "../api/generated";
import { formatDateTime, formatDuration, shortId } from "../utils";
import { EmptyState, StatusBadge } from "./ui";

export function RunTable({ runs, emptyDescription = "実行されるとここに履歴が表示されます。" }: { runs: Run[]; emptyDescription?: string }) {
  if (runs.length === 0) return <EmptyState title="Run はありません" description={emptyDescription} />;

  return (
    <div className="table-frame">
      <table className="data-table run-table">
        <thead>
          <tr>
            <th>Run</th>
            <th>Agent / Handler</th>
            <th>Trigger</th>
            <th>状態</th>
            <th>作成時刻</th>
            <th>所要時間</th>
            <th>Runtime</th>
            <th aria-label="詳細" />
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id}>
              <td data-label="Run" className="mono-cell">
                <Link className="run-id-link" to="/runs/$runId" params={{ runId: run.run_id }} title={run.run_id}>
                  {shortId(run.run_id, 12)}
                </Link>
              </td>
              <td data-label="Agent / Handler">
                <span className="stacked-cell">
                  <Link to="/agents/$agentId" params={{ agentId: run.agent_id }}>
                    <strong>{run.agent_id}</strong>
                  </Link>
                  <span>{run.handler}</span>
                </span>
              </td>
              <td data-label="Trigger">{run.source}</td>
              <td data-label="状態">
                <StatusBadge status={run.status} pulse={run.status === "running"} />
              </td>
              <td data-label="作成時刻">{formatDateTime(run.created_at)}</td>
              <td data-label="所要時間">{formatDuration(run.started_at, run.ended_at)}</td>
              <td data-label="Runtime" className="mono-cell">
                {shortId(run.runtime_instance_id)}
              </td>
              <td className="row-action">
                <Link to="/runs/$runId" params={{ runId: run.run_id }} aria-label={`Run ${run.run_id} の詳細`}>
                  <ArrowRight aria-hidden="true" size={17} />
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
