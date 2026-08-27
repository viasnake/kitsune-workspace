# Runtime Adapter

Runtime Adapter は Agent Manifest に宣言された範囲だけで Runtime Instance を起動または登録します。API や Web UI から任意 Command、Container Image、Volume を上書きすることはできません。

## Process Adapter

Process Adapter は Local Process を起動し、PID、標準出力、標準エラー、終了状態を管理します。停止時は Graceful Signal を送り、猶予後に強制終了します。Resident Process は複数 Run を Control API で受け取ります。

Ephemeral Process の SQLite Outbox は `workspace.runtime_state_directory` の Runtime Instance ごとの Directory に置かれます。Workspace の Process や Host を再起動しても回収できる耐久 Storage を指定し、Workspace 以外から変更しないでください。終端 Event を Database へ保存した後にだけ、Workspace が該当 Directory を削除します。

Workspace が管理する Process と Container には、`KITSUNE_WORKSPACE_URL`、`KITSUNE_AGENT_TOKEN`、`KITSUNE_AGENT_ID`、`KITSUNE_RUNTIME_INSTANCE_ID` を自動で渡します。Resident Runtime では `process.control_url` または `docker.control_url` から Control API の接続先と Bind Host/Port も渡します。Ephemeral Run では `KITSUNE_RUN_ID`、`KITSUNE_HANDLER`、`KITSUNE_RUN_SOURCE`、`KITSUNE_CORRELATION_ID`、任意の Parent Run ID、Trace ID、Deadline も加えます。Agent Token は Manifest の `security.agent_token_ref` から起動直前に解決します。

Process Adapter は `public_url` がなければ Workspace の Bind Address から接続 URL を組み立て、Wildcard Address は Loopback へ置き換えます。Docker Agent には、Container の Network Namespace から到達できる `workspace.public_url` が必須です。

Process の Working Directory、Command、Resident Control URL は Manifest に固定します。SDK 内蔵 Control API は TLS を終端しないため、Process の Control URL は Workspace と同じ Host の HTTP Loopback endpoint に限定します。Secret 参照は起動直前に `env://` または `file://` から解決し、Snapshot、Audit、Log に値を残しません。

## Docker Adapter

Docker Adapter は Docker Engine API を使い、Resident Container と Run ごとの Ephemeral Container を管理します。Container ID は Runtime Instance に保存し、直近 Log は Engine API から Tail します。Network、Volume、Environment は Manifest に宣言した値だけを使い、`runtime.environment` と `runtime.secrets` の参照は起動直前に解決して Container Environment へ渡します。SDK 内蔵 Control API は TLS を終端しないため、Resident Container の `docker.control_url` は、宣言した隔離 Docker Network から Workspace が到達できる HTTP endpoint にします。Host Network は使用できません。

Ephemeral Container の SQLite Outbox は Container の専用 Directory に残します。Container が終了しても Workspace はすぐ削除せず、Docker Engine から停止中の Outbox を取得し、Event ID で重複を除きながら Database へ保存します。Database を利用できなければ Container と Outbox を残して回収を再試行し、保存完了後にだけ削除します。取得した一時ファイルは `workspace.runtime_state_directory` に置くため、この Directory に耐久 Storage を指定してください。

次を既定で拒否します。

- Privileged Container
- Host Network
- Manifest 外からの Image、Command、Volume の上書き
- 相対 Host Path や不正な Mount

Docker Socket を Mount した Workspace は Host 上で Container を作成できる高権限 Service です。Socket への接続主体を Workspace Container に限定し、Workspace の OIDC、RBAC、Network、Image 更新経路を信頼境界として保護してください。一般 User や Agent Container へ Socket を渡さないでください。

## External Adapter

External Adapter は ECS、別 Host、別 Cloud など Workspace 外で起動した Agent を登録します。Workspace はその Process の Start/Stop を実行しません。SDK の登録、Heartbeat、Control API、Manual Invocation、Run、Event、Usage を扱います。

Control endpoint は HTTPS が既定です。平文 HTTP は Workspace 全体の開発設定を明示的に有効にした時だけ使えます。この設定を使う環境では、Operator が Network Policy と Bind 設定によって接続元と接続先を開発 Network に限定してください。

EventBridge などから External Ephemeral Agent を起動する場合、Task のオーケストレータが Outbox の耐久性も管理します。`KITSUNE_OUTBOX_PATH` は Task と独立して残る Volume または Network Storage に置きます。Agent が Status `75` で終了したら、同じ Storage と認証情報で `KITSUNE_OUTBOX_DRAIN_ONLY=1` を指定した回復実行を再試行し、Status `0` の後に Storage を削除します。回復実行は Handler を再実行しません。External Adapter が ECS Task を起動する機能はありません。

## Restart Policy

Resident Runtime では `never`、`on_failure`、`always` を選び、試行上限、初期 Backoff、最大 Backoff、安定稼働後の Count Reset を設定できます。異常終了が続いて試行上限に達すると Crash Loop として Runtime Instance を `failed` にし、自動起動を止めます。Operator が明示的に Restart すると Count を初期化します。Ephemeral Runtime は Run ごとに起動し、失敗した Run を Lifecycle 操作や Restart Policy で暗黙に再実行しません。
