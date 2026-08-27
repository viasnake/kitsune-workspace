# Kitsune SDK

Kitsune SDK は Agent Application の Process 内で使う Python 3.12 以上の Library です。Pydantic AI、LangChain、または通常の非同期 Callable と一緒に使えます。Workspace は必須ではありません。

## Agent Application を作る

```python
from pydantic import BaseModel

from kitsune import KitsuneApp, RunContext


class InvestigationRequest(BaseModel):
    message: str


class InvestigationResult(BaseModel):
    summary: str


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
```

公開 API は型注釈と Docstring を持ちます。Handler の Pydantic Model は Control API の Manifest と Workspace の Manual Invocation Form に使われます。

## Standalone 実行

常駐させる場合は `serve` を使います。Control API を起動するため、実行中の Process は待機します。

```bash
cd examples/standalone-agent
PYTHONPATH=. kitsune agent serve app:app
```

一回だけ直接 Handler を実行する場合は、`serve` の代わりに `once` を使います。起動済みの Control API へ依頼せず、この Process が `app:app` を読み込んで Handler を実行します。

```bash
cd examples/standalone-agent
PYTHONPATH=. kitsune agent once app:app \
  --handler echo \
  --input request.json
```

`KITSUNE_WORKSPACE_URL` を設定しなければ、Run ID と相関 ID をローカルで生成し、JSON Log と設定済みの OpenTelemetry だけを使います。Workspace の停止を理由に Handler を無期限に待たせません。

Standalone または外部 Orchestrator 管理の Agent でも、SDK は `OPENAI_API_KEY`、`AWS_SESSION_TOKEN`、`DATABASE_PASSWORD` のように Secret を示す名前の Environment を起動時に検出します。その値は Process 内だけで解決し、JSON Log、Plugin Event、SQLite Outbox の前で Agent Token と同様に除去します。Secret を示さない独自名で Credential を渡す場合だけ、`KITSUNE_REDACTED_ENVIRONMENT_VARIABLES` に Environment 名の JSON 配列を追加します。

```bash
export KITSUNE_REDACTED_ENVIRONMENT_VARIABLES='["OPAQUE_PROVIDER_VALUE","OPAQUE_MODEL_VALUE"]'
```

## Managed 実行

常駐 Agent の Control API は次を公開します。

```text
GET  /_kitsune/manifest
GET  /_kitsune/healthz
GET  /_kitsune/readyz
POST /_kitsune/runs
POST /_kitsune/runs/{run_id}/cancel
```

Run 受付は `202 Accepted` を返し、結果は Event として Workspace に送ります。Event Outbox を使う時は、SDK が開始 Event と終端 Event の二枠を受付前に確保します。容量がなければ Resident Control API は Handler を起動せず `503` を返します。一時起動 Agent は `KITSUNE_RUN_ID`、`KITSUNE_WORKSPACE_URL`、`KITSUNE_AGENT_TOKEN`、`KITSUNE_HANDLER` を受け取り、入力取得、Handler 実行、Event 送信を完了して終了します。

Workspace に接続する常駐 Agent では、Descriptor、Run 受付、Cancel に `KITSUNE_AGENT_TOKEN` の Bearer 認証が必要です。Workspace は Manifest の `security.agent_token_ref` を解決し、管理下の Process/Container へ Token と Workspace URL を注入したうえで、同じ Token で Control API を呼びます。Descriptor は認証の有無にかかわらず、JSON Schema の Property 名を保ったまま Secret に該当する説明、Default、Example、自由記述を除去して返します。Request Body は Routing 前に `KITSUNE_CONTROL_MAX_REQUEST_BYTES`（Default 1 MiB）で制限し、上限を超えた場合は `413` を返します。Workspace 未設定かつ Loopback に Bind する Standalone Agent は Token なしでも利用できます。この場合、Control API は Bind Port 上の Loopback または `localhost` の Host だけを受け付け、`Origin` Header がある Request は Bind Origin と完全に一致する場合だけ許可します。CLI など `Origin` Header を送らない Local Client は利用できます。非 Loopback Bind では Token が必須です。

Workspace 管理の Process/Container では、Manifest の `runtime.secrets` 名と Secret に該当する `runtime.environment` 名を Workspace が同じ設定へ自動注入します。Environment の実値はこの名前一覧に含めず、SDK Process 内だけで解決します。

## Run Context と子 Run

`RunContext` は Agent、Runtime Instance、Run、親 Run、相関 ID、Trigger、開始時刻、Deadline、Cancellation、Logger、Tracer、Workspace Client、Metadata を保持します。Slack Thread や HTTP Request の固有情報は `metadata` に置きます。

```python
async with ctx.child_run(name="historical-investigation") as child:
    result = await historical_agent.run(child)
```

子 Run は相関 ID、Trace Context、Deadline、Cancellation と親 Run ID を継承します。SDK は子 Agent が Local か Remote かを判断しません。

## Event Outbox

Workspace へ送る標準 Event は SQLite Outbox に保存されます。Delivery Worker は指数 Backoff で Batch を再送し、成功した Event を削除します。Queue 上限に達すると明示的な Error と `kitsune_event_outbox_size` Metric を出し、黙って Event を捨てません。Heartbeat は Best Effort で直接送信します。

Model 呼び出しの前には Usage Event 一件分も予約します。独自 Provider 連携は `RunContext.check_model_call()` が返す `ModelCallAdmission` を保持し、呼び出し失敗または Usage なしの場合は `release()`、Usage 取得時は `record_usage()` を呼びます。Pydantic AI と LangChain の公式連携はこの予約と解放を自動で行います。

Ephemeral Agent は終了時の Flush に上限時間を設けます。標準 Event が Outbox に残った場合、`kitsune agent once` は一時的な配送失敗を表す終了 Status `75` を返します。Workspace が起動する Process と Container では、この Status と永続 Outbox を使って配送を回復します。

EventBridge などが起動する External Ephemeral Agent では、オーケストレータが `KITSUNE_OUTBOX_PATH` を Task 終了後も残る Storage 上の Path に設定してください。Status `75` を受けたら、同じ Agent Image、Workspace URL、Agent Token、Outbox Path に `KITSUNE_OUTBOX_DRAIN_ONLY=1` を加えて、同じ `kitsune agent once package.module:app` を再実行します。この実行は登録、Run の Ack、Handler、標準 Event の新規生成を行わず、残っている Event だけを送ります。Workspace が復旧するまで Status `75` を Retry し、Status `0` を確認してから Storage を解放します。

## Framework 連携

`kitsune-integration-pydantic-ai` は Model 設定、Fallback、Usage 変換、Trace 接続、MCP 設定補助、TestModel を提供します。Provider 固有 Model を直接渡すこともできます。`run_agent()` は Pydantic AI の Native Span を内容と例外文なしで出力します。Agent 単位の `instrument=True` は安全な設定で上書きし、別の `Instrumentation` Capability による置き換えは受け付けません。Kitsune が OpenAI や Anthropic SDK を包むことはありません。

`kitsune-integration-langchain` は Runnable の非同期実行、Callback/Middleware の Usage と Trace、Streaming Event の進行 Event 変換を提供します。LangChain の Tool、State、Memory は再実装しません。

## Graceful Shutdown

SDK は SIGINT と SIGTERM を受けると新しい Run の受付を止め、実行中 Run を設定時間内で終了させ、Plugin を逆順に停止し、Outbox を Flush してから Process を終了します。Run の Cancel は Handler の Cancellation Scope に伝播します。
