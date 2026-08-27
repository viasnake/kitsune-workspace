# kitsune-workspace

Kitsune Agent Application の Manifest、Run、Runtime Instance、Schedule、Event を管理する
ローカルファーストのコントロールプレーンです。Process、Docker、External の各 Runtime
Adapter を同じ API から操作し、SQLite または PostgreSQL に状態を保存します。

インストール後、書き込み可能なローカルディレクトリに設定を作り、データベースを更新して Workspace を起動します。

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

Workspace は起動時に Database 上の Instance Lock を必ず取得し、同じ Workspace 名の Scheduler を一つに制限します。

運用操作も同じ CLI の `kitsune workspace` 以下から行います。設定と Agent Manifest の
書式は、プロジェクト文書の Workspace と Agent Manifest の説明を参照してください。

この wheel には API、CLI、データベースマイグレーションが含まれます。
`static_directory` には別途ビルドした Web アセットを指定できます。ビルド済み Web UI
を含む配布物は本番コンテナです。
