# 観測性

Kitsune SDK と Workspace は OpenTelemetry を Trace、Metric、Log 相関の標準にします。SDK は Trace と Metric、Workspace は Trace、Metric、Log を OTLP endpoint へ送ります。SDK の Log は相関 Field を含む JSON として標準出力へ書き、実行環境の Log Collector で収集します。Dynatrace などの Backend に対する専用の Export Protocol は実装しません。Docker Compose では OpenTelemetry Collector の Debug Exporter で受信を確認できます。

## 構造化 Log

標準出力は JSON で、利用できる範囲の次の Field を持ちます。

```text
timestamp level service agent_id runtime_instance_id run_id
parent_run_id correlation_id trace_id event message
```

Authorization Header、API Key、Bearer Token、Secret 参照の解決値を出しません。Redaction Key は追加設定でき、Object と文字列化された Header の両方へ適用します。Raw Log は Workspace DB に複製しません。

## Trace

Kitsune は次の Span を作ります。

```text
kitsune.agent.start
kitsune.run
kitsune.handler
kitsune.child_run
kitsune.workspace.dispatch
kitsune.runtime.start
kitsune.runtime.stop
kitsune.schedule.dispatch
kitsune.webhook.dispatch
```

SDK の `ctx.child_run()` は現在の OpenTelemetry Context の下で子 Run Span を作ります。Pydantic AI と LangChain の Model/Tool Span も現在の `kitsune.run` の下に接続します。Workspace は `trace_id` を Run、Event、Agent への Dispatch Payload に保持し、Manifest の `trace_url_template` から Backend Link を作ります。

## Metric

Run 数と時間、Active/Queued Run、失敗、Runtime 数と Restart、Heartbeat Age、Outbox Size と配送失敗、Model Request と Token と推定費用、Schedule Delay を記録します。`run_id`、`trace_id`、User ID のような高 Cardinality 値を Label にしません。

## OTLP 設定

SDK の Trace と Metric、および Workspace の Trace、Metric、Log は標準の OpenTelemetry Environment Variable を利用できます。

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_SERVICE_NAME=kitsune-workspace
```

Dynatrace では OTLP endpoint と認証 Header を Secret として Environment に注入します。値を Manifest や Git に書かないでください。

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://example.live.dynatrace.com/api/v2/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Api-Token%20${DYNATRACE_API_TOKEN}"
```

Collector から送る場合は `deploy/otel-collector.dynatrace.example.yaml` を起点にし、`DYNATRACE_OTLP_ENDPOINT` と `DYNATRACE_API_TOKEN` を Collector の Secret として渡します。

Langfuse を使う Agent は `kitsune-plugin-langfuse` を導入します。Plugin は入力・出力・Span Attribute の Masking を適用してから、Langfuse の OpenTelemetry 設定を有効にします。
