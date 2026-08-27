# Agent Manifest

Agent Manifest は一つの Agent Definition を宣言する YAML です。Workspace は設定 Directory のすべての Manifest を一括検証し、正常な集合だけを反映します。

```yaml
schema: kitsune.agent
revision: 1

metadata:
  id: sre-agent
  display_name: SRE Agent
  description: SRE 調査を支援する Agent
  labels:
    domain: sre
    environment: production

spec:
  runtime:
    adapter: process
    mode: resident
    desired_state: running
    restart:
      policy: on_failure
      max_attempts: 5
      backoff_seconds: 5
      max_backoff_seconds: 60
      reset_after_seconds: 300
    process:
      command: ["uv", "run", "python", "-m", "sre_agent"]
      control_url: http://127.0.0.1:8081
      working_directory: /opt/sre-agent
    environment: {}
    secrets:
      OPENAI_API_KEY: env://OPENAI_API_KEY
      SLACK_BOT_TOKEN: file:///run/secrets/slack_bot_token

  invocation:
    default_handler: investigate
    max_concurrency: 4
    queue_capacity: 20
    queue_policy: queue
    timeout_seconds: 900
    store_input: true
    store_output: true
    retention_days: 30
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
    trace_url_template: ""
    log_url_template: ""

  security:
    agent_token_ref: env://SRE_AGENT_KITSUNE_TOKEN
```

## Runtime ごとの設定

`process` では Command、Working Directory、Resident Control URL、`docker` では Image、Command、Network、Volume、Environment、Secret File、Resident Control URL、`external` では HTTPS Control URL を設定します。選択した Adapter に必要な Block だけを指定します。Resident の Process/Docker では `control_url` が必須で、Workspace はそこから SDK の接続先と Bind Host/Port を注入します。SDK 内蔵 Server を直接起動するため、Process は HTTP Loopback、Docker は宣言した隔離 Network 上の HTTP URL に限定します。Ephemeral には Control API がないため指定しません。

`privileged` と Host Network は既定で許可されず、本番 Manifest では使用できません。API の Run Input から Runtime 設定を変更することもできません。

## Secret 参照

```text
env://VARIABLE_NAME
file:///absolute/path
```

Environment 参照は Workspace Process の Environment、File 参照は Workspace が読める絶対 Path から値を取得します。Workspace が解決した値は起動する Process/Container にだけ渡し、DB、Manifest Snapshot、Audit、Error、Log には出しません。専用 Secret Manager を Kitsune が抽象化することはありません。

Workspace が起動する Process と Container には、`KITSUNE_WORKSPACE_URL` と `KITSUNE_AGENT_TOKEN` を `workspace.public_url` と `security.agent_token_ref` から注入します。同じ値を `runtime.environment` や `runtime.secrets` に重ねて宣言する必要はありません。External Agent は Workspace の管理外で起動するため、自身の実行環境に接続先と Token を設定します。

## 検証

```bash
kitsune manifest validate config/examples/agents/managed-resident-agent.yaml
kitsune manifest schema > agent-manifest.schema.json
```

Agent ID、Trigger ID、Handler 参照、Cron、Timezone、Runtime 設定、Queue、Timeout、Payload Limit、Secret URI を検証します。定義済みでない Command を後から API で指定する方法はありません。

`invocation` 直下の値は Agent 全体の既定値です。`invocation.handlers` では、名前付き Handler の `max_concurrency`、`queue_capacity`、`queue_policy` だけを上書きできます。Handler Descriptor は実行入口の Schema を報告する契約であり、運用上の受付制限は Manifest に置きます。
