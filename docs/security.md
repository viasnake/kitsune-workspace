# セキュリティ

Kitsune Workspace は Process と Container を起動できる高権限の制御面です。Internet へ直接公開せず、HTTPS、OIDC、Network Policy、Database 権限、Container Image の更新経路を一つの信頼境界として管理してください。

## 人間の認証と権限

本番は OIDC を使用します。Authorization Code Flow、PKCE、State、Nonce を検証し、Secure、HttpOnly、SameSite Cookie と CSRF Token を使います。`auth.redirect_uri` には Provider に登録した Canonical HTTPS Callback URI を必ず設定し、Authorization と Token Exchange の両方で同じ値を使います。未認証の Web UI は OIDC Login への入口を表示し、認証後の Header から Session を終了できます。`none` は Bind Address と `workspace.public_url` が Loopback の開発環境だけで許可されます。この Mode では Request の Host を設定した Port 上の Loopback または `localhost` に限定し、`Origin` Header がある場合は `workspace.public_url`、未設定なら Bind Origin との完全一致を要求します。CLI など `Origin` Header を送らない Local Client は利用できます。

| 操作 | viewer | operator | admin |
| --- | ---: | ---: | ---: |
| 状態、Run、Event、Usage の閲覧 | 可 | 可 | 可 |
| Manual Run、Cancel | 不可 | 可 | 可 |
| Resident Agent の Start、Stop、Restart | 不可 | 可 | 可 |
| Manifest Reload | 不可 | 不可 | 可 |
| Agent Token の発行と失効 | 不可 | 不可 | 可 |

Frontend の Button 制御は操作ミスを減らす表示上の補助です。すべての権限は Backend でも検証します。

SSE は Global、Principal、Remote IP ごとの同時接続数を制限します。`security.sse_max_connections`、`security.sse_max_connections_per_principal`、`security.sse_max_connections_per_ip` は、Proxy が渡す接続元と利用者数に合わせて設定してください。上限を超えた接続は Stream を開始する前に拒否され、切断時には枠を解放します。

## Agent Token

Agent ごとに十分な Entropy を持つ Bearer Token を発行します。DB には Token の SHA-256 Digest と識別 Prefix、発行・失効時刻だけを保存します。平文 Token は発行応答に一度だけ含めます。Manifest の `agent_token_ref` は認証のたびに参照先から解決し、DB には初回成功時の監査用 Salt 付き scrypt Hash だけを保存します。Token を Log、Audit Payload、Error に含めません。

## Webhook

Webhook Trigger は Shared Secret または HMAC Signature、Request Size Limit、Rate Limit、Idempotency Key を扱います。Signature は Raw Body から計算し、一定時間比較します。同じ Idempotency Key の再送は既存 Run を返します。Kitsune は Timestamp Header を規定しないため、送信元固有の Replay Window が必要な場合は Workspace の手前で検証してください。Slack や PagerDuty 固有 Payload を Core が変換することはありません。

## 入力と保存

Workspace は Request Body を上限まで Stream で読み、超過時は処理前に拒否します。管理 API は型付き Request Model、Agent API の Event Batch は共有 Contract で検証し、Handler の入力と出力は SDK が登録済み Pydantic Model で検証します。

`store_input=false` の Run でも、Workspace は Agent へ入力を渡すために実行中だけ値を保持し、終端状態へ移った時に消去します。`store_output=false` では、Run Record と Event のどちらにも出力を保存しません。保存が有効でも Size Limit を超える出力は Event へ残さず、Run を失敗として記録します。Kitsune は汎用 Artifact Store を持たないため、大きな成果物は Agent が外部 Storage に保存して参照を返します。

## Runtime の制約

- API と Web UI から任意 Shell Command を実行できません。
- Process Command、Container Image、Command、Network、Volume は Manifest に固定します。
- Docker の Privileged と Host Network は既定で拒否します。
- External Agent は HTTPS を既定にします。
- `env://` と `file://` の Secret 値を Snapshot や Log に展開しません。

Docker Socket を利用する Workspace は Host 上の Container 管理権限を持ちます。Socket を Agent Container と共有せず、Workspace への Operator/Admin Access を厳格に制限してください。

Docker から取得する Log と Outbox Archive は Stream で読み、`security.docker_log_response_max_bytes` と `security.docker_outbox_archive_max_bytes` で応答量を制限します。Outbox はさらに `docker_outbox_archive_max_members`、`docker_outbox_member_max_bytes`、`docker_outbox_total_max_bytes` を検証し、全 Member の検証が終わるまで復旧先を置き換えません。違反時は Container を削除せず Runtime を Unhealthy にして、次回の復旧を待ちます。

削除済み Container の診断 Log は `security.docker_archived_logs_max_bytes` と `security.docker_archived_log_containers` の範囲だけを Memory に保持します。

Process Log は 1 行、Runtime Instance ごとの Live Tail、終了済み Runtime 全体の Archive に独立した Byte 上限を適用します。改行なしの過大な行は最後まで読み捨て、内容を保存せず省略 Marker だけを残します。Docker Log の秘匿値集合が Workspace 再起動で失われた場合は、保存済み Runtime Hash と一致する Manifest Snapshot および Docker inspect の実 Environment から Memory 上にだけ再構築します。一致を証明できない場合は Log を返しません。

`security.redacted_keys` は追加設定です。Authorization、Cookie、API Key、Password、Secret、Token に対応する必須 Key は、空の設定を指定しても無効化できません。SDK と Workspace は、設定から解決した実 Secret 値も Log、Trace、Event、Run の永続化前に除去します。

SQLite を使う場合、SDK Outbox と Workspace Database の直上 Directory を新規作成するときは `0700` に固定します。既存 Directory は実行 User の所有と書き込み権限を検査し、変更せずに使います。途中の Directory がない Path、書き込み可能な共有 Directory、Symlink、通常 File ではない Path、実行 User が所有していない Path は拒否します。Database・WAL・SHM は `0600` に固定します。

## Audit

Token 操作、Reload、Start/Stop/Restart、Manual Run、Cancel、Agent 登録、Webhook Dispatch、Schedule Dispatch を Audit に記録します。各 Record は Actor、対象、結果、時刻を持ち、HTTP Request では Remote Address と Request ID、人間の操作では Role も保持します。Secret と保存禁止 Payload は Audit に含めません。Audit の Retention は Run/Event より長く設定できます。
