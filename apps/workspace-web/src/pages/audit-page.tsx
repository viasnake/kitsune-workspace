import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { workspaceApi } from "../api/generated";
import { queryKeys } from "../query-keys";
import { formatDateTime, humanize, stringifyJson } from "../utils";
import { EmptyState, ErrorState, LoadingBlock, PageHeader } from "../components/ui";

export function AuditPage() {
  const [search, setSearch] = useState("");
  const auditQuery = useQuery({
    queryKey: queryKeys.audit,
    queryFn: ({ signal }) => workspaceApi.listAudit(signal),
  });
  const records = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (query.length === 0) return auditQuery.data ?? [];
    return (auditQuery.data ?? []).filter((record) =>
      [record.actor_id, record.action, record.resource_type, record.resource_id ?? "", record.outcome]
        .join(" ")
        .toLocaleLowerCase()
        .includes(query),
    );
  }, [auditQuery.data, search]);

  return (
    <div className="page-stack">
      <PageHeader title="監査ログ" description="制御面への操作と拒否を、actor と resource の組み合わせで追跡します。" />
      <div className="table-toolbar">
        <label className="search-field">
          <Search aria-hidden="true" size={17} />
          <span className="sr-only">監査ログを検索</span>
          <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Actor、操作、Resource で検索" />
        </label>
        <span className="result-count" aria-live="polite">{records.length} 件</span>
      </div>
      {auditQuery.isLoading ? (
        <LoadingBlock rows={8} label="監査ログを読み込み中" />
      ) : auditQuery.error !== null ? (
        <ErrorState error={auditQuery.error} retry={() => void auditQuery.refetch()} />
      ) : records.length === 0 ? (
        <EmptyState title="監査記録がありません" description={auditQuery.data?.length === 0 ? "操作が記録されるとここに表示されます。" : "検索条件を変更してください。"} />
      ) : (
        <div className="table-frame">
          <table className="data-table audit-table">
            <thead><tr><th>時刻</th><th>Actor</th><th>Action</th><th>Resource</th><th>結果</th><th>接続元</th><th>詳細</th></tr></thead>
            <tbody>
              {records.map((record) => (
                <tr key={record.id}>
                  <td data-label="時刻">{formatDateTime(record.occurred_at)}</td>
                  <td data-label="Actor"><span className="stacked-cell"><strong>{record.actor_id}</strong><span>{record.actor_type}</span></span></td>
                  <td data-label="Action" className="mono-cell">{record.action}</td>
                  <td data-label="Resource"><span className="stacked-cell"><strong>{record.resource_type}</strong><span>{record.resource_id ?? "—"}</span></span></td>
                  <td data-label="結果"><span className={`audit-outcome audit-${record.outcome}`}>{humanize(record.outcome)}</span></td>
                  <td data-label="接続元" className="mono-cell">{record.remote_address ?? "—"}</td>
                  <td data-label="詳細">
                    <details className="inline-details"><summary>JSON</summary><pre>{stringifyJson(record.details)}</pre></details>
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
