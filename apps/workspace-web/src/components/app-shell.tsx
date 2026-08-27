import { useState } from "react";
import { Link, Outlet } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  CalendarClock,
  ChevronLeft,
  ClipboardList,
  LayoutDashboard,
  LogIn,
  LogOut,
  Menu,
  PlaySquare,
  RefreshCw,
  X,
} from "lucide-react";
import { ApiError, workspaceApi } from "../api/generated";
import { useAuth } from "../auth-context";
import { queryKeys } from "../query-keys";
import { useWorkspaceStream, type StreamConnectionState } from "../use-workspace-stream";
import { describeError, humanize } from "../utils";
import { LoadingButton, StatusBadge } from "./ui";

const navigation = [
  { to: "/", label: "ダッシュボード", icon: LayoutDashboard, exact: true },
  { to: "/agents", label: "Agent", icon: Bot, exact: false },
  { to: "/runs", label: "Run", icon: PlaySquare, exact: false },
  { to: "/schedules", label: "Schedule", icon: CalendarClock, exact: false },
  { to: "/audit", label: "監査ログ", icon: ClipboardList, exact: false },
] as const;

function StreamIndicator({ state }: { state: StreamConnectionState }) {
  const label = state === "connected" ? "ライブ更新中" : state === "connecting" ? "接続中" : "再接続中";
  return (
    <span className={`stream-indicator stream-${state}`} title="Server-Sent Events 接続状態">
      <span aria-hidden="true" />
      {label}
    </span>
  );
}

function AuthenticationGate({ error }: { error: Error | null }) {
  const returnTo = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const loginUrl = `/api/auth/login?${new URLSearchParams({ return_to: returnTo }).toString()}`;
  const authenticationRequired = error instanceof ApiError && error.status === 401;

  return (
    <main className="authentication-page">
      <section className="authentication-card" aria-labelledby="authentication-heading">
        <div className="authentication-brand" aria-hidden="true">K</div>
        <div>
          <span className="authentication-eyebrow">Kitsune Workspace</span>
          <h1 id="authentication-heading">
            {authenticationRequired ? "認証が必要です" : "Workspace に接続できません"}
          </h1>
          <p>
            {authenticationRequired
              ? "運用画面を開くには、組織の OpenID Connect Provider で認証してください。"
              : "Workspace の状態を確認してから、もう一度接続してください。"}
          </p>
        </div>
        {authenticationRequired ? (
          <a className="button button-primary authentication-action" href={loginUrl}>
            <LogIn aria-hidden="true" size={17} />
            OIDC でログイン
          </a>
        ) : (
          <button className="button button-secondary authentication-action" type="button" onClick={() => window.location.reload()}>
            <RefreshCw aria-hidden="true" size={17} />
            再接続
          </button>
        )}
        {error !== null && !authenticationRequired && (
          <p className="authentication-error" role="alert">{describeError(error)}</p>
        )}
      </section>
    </main>
  );
}

function AuthenticationLoading() {
  return (
    <main className="authentication-page" aria-live="polite">
      <section className="authentication-card authentication-loading">
        <div className="authentication-brand" aria-hidden="true">K</div>
        <div>
          <span className="authentication-eyebrow">Kitsune Workspace</span>
          <h1>セッションを確認中</h1>
          <p>Workspace への接続と認証状態を確認しています。</p>
        </div>
      </section>
    </main>
  );
}

function AuthenticatedShell() {
  const [mobileNavigationOpen, setMobileNavigationOpen] = useState(false);
  const auth = useAuth();
  const queryClient = useQueryClient();
  const streamState = useWorkspaceStream();
  const healthQuery = useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => workspaceApi.getHealth(signal),
    refetchInterval: 30_000,
  });
  const reloadMutation = useMutation({
    mutationFn: () => workspaceApi.reloadDefinitions(auth.session?.csrf_token ?? ""),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
        queryClient.invalidateQueries({ queryKey: queryKeys.agents }),
        queryClient.invalidateQueries({ queryKey: queryKeys.schedules }),
      ]);
    },
  });
  const logoutMutation = useMutation({
    mutationFn: () => workspaceApi.logout(auth.session?.csrf_token ?? ""),
    onSuccess: () => {
      window.location.assign("/");
    },
  });

  return (
    <div className="app-shell">
      <aside className={mobileNavigationOpen ? "sidebar sidebar-open" : "sidebar"} aria-label="メインナビゲーション">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            K
          </div>
          <div>
            <strong>Kitsune</strong>
            <span>Workspace</span>
          </div>
          <button
            type="button"
            className="icon-button sidebar-close"
            aria-label="ナビゲーションを閉じる"
            onClick={() => setMobileNavigationOpen(false)}
          >
            <X aria-hidden="true" size={19} />
          </button>
        </div>

        <nav className="primary-navigation">
          {navigation.map(({ to, label, icon: Icon, exact }) => (
            <Link
              key={to}
              to={to}
              activeOptions={{ exact }}
              className="nav-link"
              activeProps={{ className: "nav-link nav-link-active" }}
              onClick={() => setMobileNavigationOpen(false)}
            >
              <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
              <span>{label}</span>
            </Link>
          ))}
        </nav>

        <div className="sidebar-status">
          <div className="sidebar-status-heading">
            <Activity aria-hidden="true" size={16} />
            制御面
          </div>
          {healthQuery.data !== undefined ? (
            <>
              <StatusBadge status={healthQuery.data.status} pulse={healthQuery.data.status === "healthy"} />
              <span className="sidebar-status-detail">
                Runtime {healthQuery.data.runtimes.ready} 稼働 / {healthQuery.data.runtimes.unhealthy + healthQuery.data.runtimes.lost} 異常
              </span>
            </>
          ) : (
            <span className="sidebar-status-detail">状態を確認中</span>
          )}
        </div>
      </aside>

      {mobileNavigationOpen && (
        <button
          className="sidebar-backdrop"
          type="button"
          aria-label="ナビゲーションを閉じる"
          onClick={() => setMobileNavigationOpen(false)}
        />
      )}

      <div className="workspace-column" inert={mobileNavigationOpen}>
        <header className="topbar">
          <button
            type="button"
            className="icon-button menu-button"
            aria-label="ナビゲーションを開く"
            onClick={() => setMobileNavigationOpen(true)}
          >
            <Menu aria-hidden="true" size={20} />
          </button>
          <div className="topbar-context">
            <span className="environment-pill">default</span>
            <ChevronLeft aria-hidden="true" size={14} />
            <span>Active control plane</span>
          </div>
          <div className="topbar-actions">
            <StreamIndicator state={streamState} />
            {auth.can("admin") && (
              <LoadingButton
                className="button button-quiet button-small topbar-reload"
                type="button"
                loading={reloadMutation.isPending}
                onClick={() => reloadMutation.mutate()}
              >
                <RefreshCw aria-hidden="true" size={15} />
                Manifest 再読込
              </LoadingButton>
            )}
            <div className="user-chip" title={auth.session?.subject ?? undefined}>
              <span className="user-avatar" aria-hidden="true">
                {(auth.session?.name ?? "?").slice(0, 1).toUpperCase()}
              </span>
              <span>
                <strong>{auth.isLoading ? "確認中" : (auth.session?.name ?? "未認証")}</strong>
                <small>{humanize(auth.role)}</small>
              </span>
            </div>
            <LoadingButton
              className="button button-quiet button-small topbar-logout"
              type="button"
              loading={logoutMutation.isPending}
              onClick={() => logoutMutation.mutate()}
            >
              <LogOut aria-hidden="true" size={15} />
              ログアウト
            </LoadingButton>
          </div>
        </header>

        {auth.error !== null && (
          <div className="global-banner global-banner-danger" role="alert">
            セッションを確認できません: {describeError(auth.error)}
          </div>
        )}
        {reloadMutation.error !== null && (
          <div className="global-banner global-banner-danger" role="alert">
            Manifest を再読み込みできません: {describeError(reloadMutation.error)}
          </div>
        )}
        {logoutMutation.error !== null && (
          <div className="global-banner global-banner-danger" role="alert">
            ログアウトできません: {describeError(logoutMutation.error)}
          </div>
        )}
        {reloadMutation.isSuccess && (
          <div className="global-banner global-banner-success" role="status">
            {reloadMutation.data.loaded} 件の Agent Definition を再読み込みしました。
          </div>
        )}

        <main id="main-content" className="main-content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export function AppShell() {
  const auth = useAuth();
  if (auth.isLoading) return <AuthenticationLoading />;
  if (auth.session === null) return <AuthenticationGate error={auth.error} />;
  return <AuthenticatedShell />;
}
