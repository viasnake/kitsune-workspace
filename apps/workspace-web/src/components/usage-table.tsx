import type { UsageRecord } from "../api/generated";
import { formatCost, formatNumber } from "../utils";
import { EmptyState } from "./ui";

export function UsageTable({ usage }: { usage: UsageRecord[] }) {
  if (usage.length === 0) {
    return <EmptyState title="Usage はありません" description="SDK が報告した利用量だけが記録されます。未取得値は推測しません。" />;
  }
  return (
    <div className="table-frame">
      <table className="data-table usage-table">
        <thead>
          <tr>
            <th>Provider / Model</th><th>Request</th><th>Input</th><th>Output</th><th>Total</th><th>Cache Read / Write</th><th>推定費用</th>
          </tr>
        </thead>
        <tbody>
          {usage.map((record) => (
            <tr key={record.id}>
              <td data-label="Provider / Model"><span className="stacked-cell"><strong>{record.provider ?? "未報告"}</strong><span>{record.model ?? "—"}</span></span></td>
              <td data-label="Request" className="numeric-cell">{formatNumber(record.request_count)}</td>
              <td data-label="Input" className="numeric-cell">{formatNumber(record.input_tokens)}</td>
              <td data-label="Output" className="numeric-cell">{formatNumber(record.output_tokens)}</td>
              <td data-label="Total" className="numeric-cell">{formatNumber(record.total_tokens)}</td>
              <td data-label="Cache Read / Write" className="numeric-cell">{formatNumber(record.cache_read_tokens)} / {formatNumber(record.cache_write_tokens)}</td>
              <td data-label="推定費用">{formatCost(record.estimated_cost, record.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
