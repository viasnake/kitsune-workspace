# Kitsune

Kitsune は、複数の AI Agent を開発・配備・運用する時に繰り返す処理を標準化する基盤です。Agent Application 内で使う **Kitsune SDK** と、複数 Agent を外側から管理する **Kitsune Workspace** で構成されます。

```text
Kitsune SDK       Agent Application を作りやすくする
Kitsune Workspace Agent Application 群を運用しやすくする
```

Pydantic AI、LangChain、独自の非同期 Callable をそのまま利用できます。Kitsune は Agent Loop、Provider API、Tool Protocol、MCP、RAG、Prompt 管理、外部通信を再実装しません。

## SDK を使う

Python 3.12 以上と uv を使います。

```bash
uv add kitsune-sdk
```

```python
from pydantic import BaseModel

from kitsune import KitsuneApp, RunContext


class InvestigationRequest(BaseModel):
    message: str


class InvestigationResult(BaseModel):
    summary: str


class RefreshRequest(BaseModel):
    scope: str = "all"


class RefreshResult(BaseModel):
    refreshed_scope: str


app = KitsuneApp(agent_id="sre-agent", version="1.0.0")


@app.handler(
    "investigate",
    input_model=InvestigationRequest,
    output_model=InvestigationResult,
)
async def investigate(
    ctx: RunContext,
    request: InvestigationRequest,
) -> InvestigationResult:
    ctx.logger.info("investigation requested")
    return InvestigationResult(summary=request.message)


@app.handler("refresh", input_model=RefreshRequest, output_model=RefreshResult)
async def refresh(ctx: RunContext, request: RefreshRequest) -> RefreshResult:
    ctx.logger.info("refresh requested", extra={"scope": request.scope})
    return RefreshResult(refreshed_scope=request.scope)
```

Workspace なしで一回実行できます。

```bash
cd examples/standalone-agent
PYTHONPATH=. kitsune agent once app:app \
  --handler echo \
  --input request.json
```

常駐させる場合は次を実行します。

```bash
cd examples/standalone-agent
PYTHONPATH=. kitsune agent serve app:app
```

Workspace URL が未設定でも、SDK は Run ID と相関 ID を作り、構造化 JSON Log と設定済みの OpenTelemetry へ出力します。

## Workspace を起動する

Workspace Package も共通の `kitsune` コマンドを使います。

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

Python wheel には API、CLI、Database Migration が含まれます。Build 済み Web UI を含む完成配布物は Workspace Container です。wheel から Web アセットを別に配信する場合は、Build 結果を `workspace.static_directory` に指定します。

PostgreSQL は `serve` の前に `migrate` が必須です。Local 開発用 SQLite は空の Database を自動作成できますが、配布 Migration の確認と同じ起動手順を使うため、上の Command を実行できます。

Workspace は起動時に Database 上の Instance Lock を必ず取得し、同じ Workspace 名で複数の Scheduler が同時に動くことを拒否します。

Docker Engine と Compose Plugin があれば、PostgreSQL、OpenTelemetry Collector、Web UI、Resident、Ephemeral、External の各 Agent をまとめて確認できます。

```bash
docker compose -f deploy/docker-compose.yml up --build
```

`http://127.0.0.1:8080` を開くと、Agent、Runtime Instance、Run、Schedule、Event、Usage、Audit を確認できます。Compose の認証なし設定は Loopback にだけ Bind する開発用です。

## Agent Manifest

Agent Definition の Source of Truth は YAML です。Web UI から定義自体は編集しません。

```yaml
schema: kitsune.agent
revision: 1

metadata:
  id: sre-agent
  display_name: SRE Agent
  description: SRE 調査を支援する Agent

spec:
  runtime:
    adapter: process
    mode: resident
    desired_state: running
    restart:
      policy: on_failure
      max_attempts: 5
      backoff_seconds: 5
    process:
      command: ["uv", "run", "python", "-m", "sre_agent"]
      control_url: http://127.0.0.1:8081
      working_directory: /opt/sre-agent

  invocation:
    default_handler: investigate
    max_concurrency: 4
    queue_capacity: 20
    queue_policy: queue
    timeout_seconds: 900
    handlers:
      investigate:
        max_concurrency: 2
        queue_capacity: 8
        queue_policy: queue

  triggers:
    - id: manual
      type: on_demand
      handler: investigate
    - id: periodic-refresh
      type: schedule
      handler: refresh
      cron: "0 */6 * * *"
      timezone: Asia/Tokyo
      overlap: skip
      misfire_grace_seconds: 300

  observability:
    service_name: sre-agent

  security:
    agent_token_ref: env://SRE_AGENT_KITSUNE_TOKEN
```

```bash
kitsune manifest validate config/examples/agents/managed-resident-agent.yaml
```

Reload は SIGHUP、Admin API、`kitsune workspace reload` のいずれでも実行できます。全 Manifest を先に検証し、一つでも無効なら現在の有効な集合を維持します。

Workspace が起動する Process/Container には、`KITSUNE_WORKSPACE_URL`、`KITSUNE_AGENT_TOKEN`、Agent ID、Runtime Instance ID を自動で渡します。Resident では `process.control_url` または `docker.control_url` から Control API の接続先と Bind Host/Port も渡します。Ephemeral Run では Run ID、Handler、Source、Correlation ID、Deadline も渡します。Agent Token は Manifest の `security.agent_token_ref` から起動直前に解決します。Docker Agent から到達できる Workspace URL は Workspace 設定の `public_url` に指定してください。

## Manual Run と Schedule

Web UI の Agent 詳細で Handler を選ぶと、報告された入力 JSON Schema から Form を生成します。Schema がない Handler では JSON Editor を使います。開始後は SSE で状態、進行 Event、Output、Error、Usage、Trace Link を更新します。

Schedule は Manifest に Cron、Timezone、Handler、Overlap Policy を宣言します。`allow`、`skip`、`queue`、`replace` を選べます。一時起動 Agent では Workspace が Run ID と入力を渡して Runtime を起動し、Agent は結果 Event を送って終了します。

Run は Runtime Instance と別に保存されます。一回の Run は `succeeded`、`failed`、`cancelled`、`timed_out` のいずれか一つで終わり、親子関係、相関 ID、Trace ID を保持します。Handler ごとの差分は `spec.invocation.handlers.<handler>` で設定し、省略した Field は Agent 全体の値を使います。

## Pydantic AI と LangChain

Pydantic AI 連携は Model 設定、Fallback、Usage 変換、Trace 接続、MCP 設定補助、TestModel を提供します。

```bash
uv add kitsune-integration-pydantic-ai
```

```python
from kitsune_pydantic_ai import resolve_model

model = resolve_model(
    primary="openai:configured-model",
    fallbacks=["anthropic:configured-model"],
)
```

Model 名は Kitsune に固定されません。Provider 固有 Model を直接渡すこともできます。

LangChain 連携は Runnable の非同期実行、Run Context、Callback/Middleware の Usage と Trace、Streaming Event の変換を提供します。LangChain の Tool、State、Memory はそのまま使います。

```bash
uv add kitsune-integration-langchain
```

## Plugin

Plugin は Application 全体の横断機能を加えます。LLM Tool ではありません。登録は起動前に確定し、依存循環を検出します。非 Critical Plugin の観測失敗は Run から分離し、失敗 Event として記録します。

`kitsune-plugin-budget` は Wall Clock、Model Request、Token、推定費用、子 Run の Soft/Hard Limit を扱います。`kitsune-plugin-langfuse` は Kitsune の OpenTelemetry Trace を保ったまま Langfuse へ接続し、入出力と Span Attribute の Masking を設定できます。

Plugin の作り方は [プラグイン](docs/plugins.md) を参照してください。

## OpenTelemetry、Dynatrace、Langfuse

SDK は Trace と Metric、Workspace は Trace、Metric、Log を標準 OTLP で送信します。SDK の Log は Trace ID と Run ID を含む JSON として標準出力へ書き、実行環境の Log Collector で収集します。

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

Dynatrace は OTLP endpoint として Collector または SDK/Workspace に設定します。専用の Dynatrace 抽象化はありません。API Token は Environment/Secret として注入し、Manifest や Git に保存しません。Langfuse は任意の Plugin であり、無効時も Agent コードは変わりません。

Metric に Run ID のような高 Cardinality 値を付けません。Workspace は Raw Log を DB に複製せず、Process/Docker の Tail または External Log URL を表示します。

## Security と本番配備

Workspace は Agent Process と Container を起動できる高権限の制御面です。本番では HTTPS と OIDC を必須とし、`viewer`、`operator`、`admin` の権限を Backend で検証します。Agent API は Agent ごとの Bearer Token を使い、DB には Hash だけを保存します。

任意 Shell Command は実行できません。Process Command、Container Image、Network、Volume は事前に Manifest へ宣言します。Docker の Privileged と Host Network は既定で拒否します。Webhook は HMAC、Size Limit、Rate Limit、Idempotency を検証します。Secret は `env://` または `file://` で参照し、Workspace が解決した値を Log、Event、Audit へ残しません。

本番では PostgreSQL、OIDC、HTTPS Reverse Proxy、OpenTelemetry Collector を使い、同じ Database に対する Active Workspace を一個にします。詳しくは [セキュリティ](docs/security.md) と [配備](docs/deployment.md) を参照してください。

## 文書

- [アーキテクチャ](docs/architecture.md)
- [概念と状態](docs/concepts.md)
- [共有契約](docs/contracts.md)
- [Kitsune SDK](docs/sdk.md)
- [プラグイン](docs/plugins.md)
- [Kitsune Workspace](docs/workspace.md)
- [Runtime Adapter](docs/runtime-adapters.md)
- [Agent Manifest](docs/agent-manifest.md)
- [観測性](docs/observability.md)
- [セキュリティ](docs/security.md)
- [配備](docs/deployment.md)
- [開発](docs/development.md)
- [非目標](docs/non-goals.md)

ライセンスは [Apache License 2.0](LICENSE) です。
