# Kitsune Workspace

Kitsune Workspace は複数の Agent Application を一つの Active Control Plane から管理する FastAPI Service です。Agent Definition、Runtime Instance、Run、Schedule、Event、Usage、Audit を扱い、React の運用 UI と REST API を同じ Container から配信します。

## 起動

```bash
uv tool install kitsune-workspace
mkdir -p "$PWD/kitsune-agents" "$PWD/.kitsune-workspace/runtime"
cat > "$PWD/workspace.toml" <<EOF
[workspace]
bind = "127.0.0.1:8080"
public_url = "http://127.0.0.1:8080"
database_url = "sqlite:///$PWD/.kitsune-workspace/workspace.sqlite3"
agent_manifest_directory = "$PWD/kitsune-agents"
runtime_state_directory = "$PWD/.kitsune-workspace/runtime"
EOF
kitsune workspace migrate --config "$PWD/workspace.toml"
kitsune workspace serve --config "$PWD/workspace.toml"
```

Python wheel は API、CLI、Database Migration を含みます。Build 済み Web UI は本番 Workspace Container に含まれます。Frontend を別に Build して wheel の API Process から配信する場合は、その出力 Directory を `workspace.static_directory` に設定します。

設定は Pydantic Settings で検証します。Database URL、Manifest Directory、認証設定などに誤りがある場合、暗黙の Default で続行せず起動を失敗させます。SQLite と PostgreSQL は同じ SQLAlchemy Model と配布 Package 内の Migration を使います。PostgreSQL は `serve` の前に同じ設定で `migrate` を実行しないと起動しません。Local 開発用 SQLite は空の Database を自動作成でき、配布 Migration の確認には同じ `migrate` Command を使えます。本番では PostgreSQL を推奨します。

Workspace は起動時に Database 上の Instance Lock を必ず取得します。同じ Workspace 名の Lock が有効な間は二つ目の Process を起動せず、Lock の更新に失敗した Process は Scheduler と Runtime 操作を停止します。

`workspace.public_url` は、Workspace が起動する Agent から Control Plane へ戻るための URL です。Managed Docker Agent では必須で、Agent Container の Network Namespace から到達できる URL を設定します。本番では HTTPS、認証なしの開発環境では Loopback 上の HTTP を使います。`auth.mode=none` では Bind Address と `public_url` の両方が Loopback に制限され、Request の Host と、存在する場合の `Origin` Header も検証されます。Workspace URL と Agent Token は Runtime 起動時に自動注入するため、`runtime.environment` や `runtime.secrets` へ重複して宣言しません。Resident の Process/Docker は Manifest に型付き `control_url` を宣言し、Workspace はその値から SDK の Control API 接続先と Bind Host/Port を注入します。この直接接続は SDK 内蔵 Server へ向くため、Process では HTTP Loopback、Docker では宣言した隔離 Network 上の HTTP を使います。

`workspace.runtime_state_directory` は Managed Ephemeral Runtime の送信待ち Event を回収する Workspace 専用 Directory です。Process の Outbox と、停止した Container から取得した Outbox を置くため、本番では Workspace の再起動や再作成後も残る耐久 Storage を指定します。Workspace は終端 Event の保存が完了するまで該当 Runtime の Outbox を削除しません。

## Manifest の再読み込み

Agent Definition は設定した Directory の `*.yaml` を Source of Truth とします。

```bash
kitsune workspace reload
```

SIGHUP と `POST /api/admin/reload` でも同じ Reload を実行できます。全 Manifest を一度 Memory 上で検証し、一つでも無効なら書き込みを行わず、現在有効な Definition を維持します。正常時だけ Snapshot、Hash、Trigger、Schedule を一つの Transaction で更新します。

## API

人間向け API は Agent、Runtime Instance、Handler、Run、Event、Usage、Schedule、Audit、Resident Agent の Start/Stop/Restart、Manual Invocation、Cancel、Reload を提供します。Ephemeral Runtime は Run ごとに起動するため Agent Lifecycle 操作の対象にせず、実行中の処理は Run の Cancel で止めます。`GET /api/stream` は Run、Runtime、Event 更新を Server-Sent Events で送ります。

Agent 向け API は登録、Heartbeat、Event Batch、Self Run 開始、Ephemeral Run 入力取得、Ack を提供します。Agent ごとの Bearer Token を使い、平文 Token は保存しません。

OpenAPI は `/openapi.json`、Swagger UI は開発設定時の `/docs` で確認できます。

## Queue と同時実行

Agent 全体の `max_concurrency`、`queue_capacity`、`queue_policy` は `spec.invocation` に設定します。Handler ごとの差分は `spec.invocation.handlers.<handler>` に同じ Field を必要な分だけ設定し、省略した Field は Agent 全体の値を使います。Workspace が作る Run では、`queue` は空きが出るまで Run を保持し、`reject` は即座に容量 Error を返します。Agent が開始する `self` / `child` Run は受付後すぐ実行する Protocol のため Queue には入らず、Agent または Handler の実行枠が満杯なら HTTP 429 で拒否します。容量を超えた要求を黙って破棄しません。

Schedule の重複は `allow`、`skip`、`queue`、`replace` から選びます。`replace` は同じ Schedule の前の Run を Cancel して新しい Run を作ります。Cron は Manifest の Timezone で評価し、Misfire Grace を超えた実行を遡って作りません。長期停止からの復旧時は、古い各 Occurrence を Poll ごとに処理せず、Cursor を現在時刻より後の最初の Occurrence まで進めます。

## Timeout と Cancel

各 Run は Deadline を持ちます。Deadline を超えると Resident Agent の Control API に Cancel を送り、Ephemeral Process/Container には終了要求を送ります。Grace Period 後も終了しなければ Runtime Adapter が強制終了し、Run を `timed_out` にします。Operator の Cancel は同じ経路を通り `cancelled` になります。

## Retention

Retention Job は設定した期限を過ぎた Run、Event、Usage、Audit、Runtime 履歴を Transaction 内で削除します。Audit は別の長い期限を設定できます。進行中 Run と稼働中 Runtime Instance は削除しません。

## CLI

Workspace CLI は REST Client として Agent の一覧・詳細、Resident Agent の Start/Stop/Restart、Run の一覧・詳細・Cancel、Schedule 一覧、Token 発行・失効を扱います。Reload と Token 操作は Admin 権限が必要です。Token の平文は発行時に一度だけ表示されます。

接続先は `KITSUNE_WORKSPACE_URL` で指定します。OIDC を使う場合は、Browser で認証した Session Cookie を `KITSUNE_WORKSPACE_SESSION` に Cookie Header の値として渡し、状態を変更する操作では `KITSUNE_CSRF_TOKEN` も設定します。
