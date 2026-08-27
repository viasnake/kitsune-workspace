import type { ButtonHTMLAttributes, ReactNode } from "react";
import { AlertCircle, ArrowUpRight, Inbox, LoaderCircle, RefreshCw, ShieldAlert } from "lucide-react";
import { ApiError, type RunStatus, type RuntimeStatus, type ScheduleOutcome, type Severity } from "../api/generated";
import { describeError, humanize, statusTone, stringifyJson } from "../utils";

export function StatusBadge({
  status,
  pulse = false,
}: {
  status:
    | RunStatus
    | RuntimeStatus
    | ScheduleOutcome
    | Severity
    | "healthy"
    | "degraded"
    | "unhealthy"
    | "unknown"
    | "enabled"
    | "disabled";
  pulse?: boolean;
}) {
  return (
    <span className={`status-badge status-${statusTone(status)}`}>
      <span className={pulse ? "status-dot status-dot-pulse" : "status-dot"} aria-hidden="true" />
      {humanize(status)}
    </span>
  );
}

export function ErrorState({ error, retry, compact = false }: { error: unknown; retry?: () => void; compact?: boolean }) {
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div className={compact ? "error-state error-state-compact" : "error-state"} role="alert">
      <AlertCircle aria-hidden="true" size={20} />
      <div>
        <strong>データを取得できませんでした</strong>
        <p>{describeError(error)}</p>
        {requestId !== null && <small>Request ID: {requestId}</small>}
      </div>
      {retry !== undefined && (
        <button type="button" className="button button-secondary button-small" onClick={retry}>
          <RefreshCw aria-hidden="true" size={15} />
          再試行
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <Inbox aria-hidden="true" size={28} />
      <strong>{title}</strong>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function LoadingBlock({ rows = 4, label = "読み込み中" }: { rows?: number; label?: string }) {
  return (
    <div className="loading-block" role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, index) => (
        <span className="skeleton-line" key={index} style={{ width: `${String(92 - index * 7)}%` }} />
      ))}
    </div>
  );
}

export function LoadingButton({ loading, children, disabled, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { loading: boolean }) {
  return (
    <button {...props} disabled={disabled === true || loading}>
      {loading && <LoaderCircle className="spin" aria-hidden="true" size={16} />}
      {children}
    </button>
  );
}

export function PageHeader({
  title,
  description,
  eyebrow,
  actions,
}: {
  title: string;
  description?: string;
  eyebrow?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow !== undefined && <div className="page-breadcrumb">{eyebrow}</div>}
        <h1>{title}</h1>
        {description !== undefined && <p>{description}</p>}
      </div>
      {actions !== undefined && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function SectionHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <header className="section-header">
      <div>
        <h2>{title}</h2>
        {description !== undefined && <p>{description}</p>}
      </div>
      {action}
    </header>
  );
}

export function JsonBlock({ value, label }: { value: unknown; label: string }) {
  return (
    <div className="json-block">
      <div className="json-block-label">{label}</div>
      <pre tabIndex={0}>{stringifyJson(value) || "保存されていません"}</pre>
    </div>
  );
}

export function ExternalLink({ href, children }: { href: string | null | undefined; children: ReactNode }) {
  if (href === null || href === undefined || href.length === 0) return <span className="text-muted">未設定</span>;
  return (
    <a className="external-link" href={href} target="_blank" rel="noreferrer">
      {children}
      <ArrowUpRight aria-hidden="true" size={14} />
    </a>
  );
}

export function PermissionHint({ children }: { children: ReactNode }) {
  return (
    <span className="permission-hint" title="この操作を行う権限がありません">
      <ShieldAlert aria-hidden="true" size={14} />
      {children}
    </span>
  );
}

export function DefinitionList({ children }: { children: ReactNode }) {
  return <dl className="definition-list">{children}</dl>;
}

export function Definition({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  );
}
