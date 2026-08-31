import type { RunStatus, RuntimeStatus, ScheduleOutcome, Severity, UserRole } from "./api/generated";

const dateTimeFormatter = new Intl.DateTimeFormat("ja-JP", {
  dateStyle: "medium",
  timeStyle: "medium",
});

const compactNumberFormatter = new Intl.NumberFormat("ja-JP", {
  notation: "compact",
  maximumFractionDigits: 1,
});

export const formatDateTime = (value: string | null | undefined): string => {
  if (value === null || value === undefined) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : dateTimeFormatter.format(date);
};

export const formatRelativeTime = (value: string | null | undefined, now = Date.now()): string => {
  if (value === null || value === undefined) return "未受信";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return value;
  const seconds = Math.round((time - now) / 1000);
  const formatter = new Intl.RelativeTimeFormat("ja-JP", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatter.format(Math.round(hours / 24), "day");
};

export const formatNumber = (value: number | null | undefined): string =>
  value === null || value === undefined ? "—" : compactNumberFormatter.format(value);

export const formatCost = (value: number | string | null | undefined, currency: string | null | undefined): string => {
  if (value === null || value === undefined) return "取得なし";
  if (currency === null || currency === undefined) return String(value);
  const numericValue = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(numericValue)) return `${String(value)} ${currency}`;
  try {
    return new Intl.NumberFormat("ja-JP", {
      style: "currency",
      currency,
      maximumFractionDigits: 4,
    }).format(numericValue);
  } catch {
    return `${numericValue.toFixed(4)} ${currency}`;
  }
};

export const formatDuration = (startedAt: string | null, endedAt: string | null, now = Date.now()): string => {
  if (startedAt === null) return "—";
  const start = new Date(startedAt).getTime();
  const end = endedAt === null ? now : new Date(endedAt).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return "—";
  const milliseconds = Math.max(0, end - start);
  if (milliseconds < 1000) return `${String(milliseconds)} ms`;
  const seconds = Math.floor(milliseconds / 1000);
  if (seconds < 60) return `${String(seconds)} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) return `${String(minutes)} 分 ${String(remainingSeconds)} 秒`;
  const hours = Math.floor(minutes / 60);
  return `${String(hours)} 時間 ${String(minutes % 60)} 分`;
};

const roleRank: Record<UserRole, number> = { viewer: 0, operator: 1, admin: 2 };

export const RUN_STATUSES = [
  "created",
  "queued",
  "dispatching",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
] satisfies readonly RunStatus[];

export const isListedValue = <Value extends string>(
  values: readonly Value[],
  candidate: string,
): candidate is Value => values.some((value) => value === candidate);

export const hasMinimumRole = (roles: UserRole[], minimum: UserRole): boolean =>
  roles.some((role) => roleRank[role] >= roleRank[minimum]);

export const highestRole = (roles: UserRole[]): UserRole => {
  if (roles.includes("admin")) return "admin";
  if (roles.includes("operator")) return "operator";
  return "viewer";
};

export const isTerminalRunStatus = (status: RunStatus): boolean =>
  status === "succeeded" || status === "failed" || status === "cancelled" || status === "timed_out";

export const statusTone = (
  status: RunStatus | RuntimeStatus | ScheduleOutcome | Severity | "healthy" | "degraded" | "unhealthy" | "unknown" | "enabled" | "disabled",
): "danger" | "info" | "muted" | "success" | "warning" => {
  if (status === "succeeded" || status === "ready" || status === "healthy" || status === "enabled") return "success";
  if (
    status === "failed" ||
    status === "lost" ||
    status === "error" ||
    status === "critical" ||
    status === "unhealthy" ||
    status === "timed_out"
    || status === "queue_full"
  ) return "danger";
  if (
    status === "warning" ||
    status === "degraded" ||
    status === "stopping" ||
    status === "cancelled" ||
    status === "disabled" ||
    status === "misfire_skipped" ||
    status === "overlap_skipped"
  ) return "warning";
  if (status === "running" || status === "dispatching" || status === "starting" || status === "info") return "info";
  return "muted";
};

const labels: Record<string, string> = {
  admin: "管理者",
  allow: "並行実行",
  cancelled: "キャンセル済み",
  child: "子 Run",
  created: "作成済み",
  critical: "重大",
  degraded: "低下",
  disabled: "無効",
  dispatching: "割り当て中",
  docker: "Docker",
  enabled: "有効",
  ephemeral: "一時起動",
  error: "エラー",
  external: "External",
  failed: "失敗",
  healthy: "正常",
  info: "情報",
  lost: "応答なし",
  on_demand: "手動",
  operator: "運用者",
  pending: "待機中",
  process: "Process",
  queue: "キュー",
  queued: "キュー待ち",
  ready: "稼働中",
  reload: "再読込",
  replace: "置換",
  resident: "常駐",
  running: "実行中",
  schedule: "Schedule",
  self: "自己起動",
  skip: "スキップ",
  started: "開始",
  starting: "起動中",
  stopped: "停止",
  stopping: "停止中",
  succeeded: "成功",
  timed_out: "タイムアウト",
  unhealthy: "異常",
  unknown: "不明",
  viewer: "閲覧者",
  warning: "警告",
  webhook: "Webhook",
};

export const humanize = (value: string): string => labels[value] ?? value.replaceAll("_", " ");

export const stringifyJson = (value: unknown): string =>
  value === undefined ? "" : JSON.stringify(value, null, 2);

export const describeError = (error: unknown): string => {
  if (error instanceof Error) return error.message;
  return "予期しないエラーが発生しました。";
};

export const shortId = (value: string | null | undefined, length = 8): string => {
  if (value === null || value === undefined) return "—";
  return value.length <= length ? value : `${value.slice(0, length)}…`;
};
