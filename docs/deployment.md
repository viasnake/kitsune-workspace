# 配備

Kitsune Workspace の本番構成は、Workspace Backend と Build 済み Web UI、PostgreSQL、OpenTelemetry Collector、HTTPS Reverse Proxy から成ります。同じ Database に対する Active Workspace は一個だけにします。

## Docker Compose で確認する

```bash
docker compose -f deploy/docker-compose.yml up --build
```

Compose は次を起動します。

- Workspace Backend と Web UI
- PostgreSQL
- OpenTelemetry Collector
- Managed Resident Agent
- Schedule から起動する Ephemeral Agent Image
- External Agent

開発用 UI は `http://127.0.0.1:8080` で開きます。この構成だけは Loopback bind と `auth.mode = "none"` を使います。Volume を削除せず Workspace Container だけを再起動すると、Run 履歴が PostgreSQL に残ることを確認できます。External Agent の SQLite Outbox も `external-agent-data` Volume に保存されるため、Container を作り直しても送信待ち Event を引き継ぎます。

停止は次のコマンドを使います。

```bash
docker compose -f deploy/docker-compose.yml down
```

Database Volume も削除する場合だけ、保存内容が不要であることを確認して `--volumes` を指定してください。

## 本番設定

`deploy/workspace.example.toml` を Secret 管理対象の設定へコピーし、少なくとも次を変更します。

1. `database_url` を専用 PostgreSQL User の URL にする。
2. `auth.mode = "oidc"` とし、Issuer、Client ID、Client Secret 参照、Role Claim、既定 Role を設定する。
3. `bind` は Reverse Proxy または Private Network からだけ到達できる Address にする。
4. `public_url` を Managed Agent から到達できる HTTPS URL にする。
5. `runtime_state_directory` を Workspace だけが書き込める耐久 Volume に置く。
6. External Agent の平文 HTTP を許可しない。
7. Payload と Retention の Limit を決める。
8. OTLP endpoint、TLS、認証 Header を Environment から設定する。
9. Agent Token、Webhook Secret、Provider Credential を Environment または File で注入する。

設定 Validation に失敗した Workspace は起動しません。起動 Error を修正し、Default へ暗黙に戻さないでください。

## Migration

Container 起動前に同じ Image の配布 Package に含まれる Migration を一度実行します。

```bash
kitsune workspace migrate --config /etc/kitsune/workspace.toml
```

配布 Container の Entry Point は `KITSUNE_WORKSPACE_CONFIG` が指す TOML を `migrate --config` と `serve --config` の両方に使い、Migration と Server の接続先を揃えます。wheel から実行する場合も、両 Command に同じ TOML を渡します。

複数 Replica から同時に Migration しません。Schema を更新する Release では Database Backup、Migration、Workspace 更新の順に実行します。Downgrade の可否は対象 Migration を確認し、Application Rollback だけで戻せると仮定しないでください。

## Instance Lock と復旧

Workspace は起動時に Instance Lock を取得します。別の Active Instance が Lock を保持していれば起動を拒否します。障害復旧では前の Process が停止したことと Lock の期限を確認してから次の Instance を起動します。二つの Scheduler を同時に動かす構成はサポートしません。

PostgreSQL の定期 Backup と Restore Test を運用に含めます。Kitsune が保存しない Raw Log、Artifact、Agent Memory は、それぞれの外部 Backend で Backup と Retention を管理します。

## Container と Docker Socket

Workspace Image は Build 済み Frontend を Backend の Static Directory に含めます。Runtime Adapter に Docker を使う場合だけ Engine Socket を Mount します。Socket は Host 相当の管理権限を与えるため、Workspace Container の User、Network、Image、OIDC/RBAC を保護し、Agent Container には渡しません。

Docker Adapter が不要な環境では Socket を Mount せず、Process または External Adapter だけを有効にしてください。

## OpenTelemetry Backend

Collector へ OTLP を送り、Collector 側で Dynatrace などの Backend へ Export します。Backend Token は Collector の Secret として注入します。`deploy/otel-collector.yaml` の Debug Exporter は確認用であり、本番では認証付き Exporter と Queue/Retry 設定に置き換えます。

## Release Artifact

Release Workflow は一つの Tag と Commit から全 Python Package と Workspace Container を Build します。Python Package は GitHub OIDC による PyPI Trusted Publishing で公開します。Workspace Container は `packages: write` に限定した `GITHUB_TOKEN` で GHCR へ公開し、公開した Digest に Build Provenance Attestation を付けます。Container Job の `id-token: write` は Attestation の署名に使い、GHCR の認証には使いません。PyPI の `pypi` Environment と各配布 Package の Trusted Publisher を、公開前に PyPI 側で登録してください。
