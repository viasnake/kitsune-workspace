# Kitsune SDK

## 1. 目的

Kitsune SDK は、AI Agent の知能を実装する Framework ではなく、**Agent Application を構成するための共通骨格**を提供する。

Pydantic AI、LangChain、独自実装などの上位または周囲で使う。

## 2. SDK が提供するもの

### アプリケーション骨格

- 起動
- graceful shutdown
- 設定
- 実行コンテキスト
- エラーの共通処理

### 実行情報

一回の Agent 実行に対して最低限以下を持つ。

```text
RunContext
├── run_id
├── agent_id
├── started_at
├── correlation_id
├── source
├── logger
└── cancellation
```

Agent 固有の conversation や Slack thread を Core の共通型にはしない。

### 標準イベント送出

SDK は最低限の運用イベントを生成する。

```text
agent.started
agent.ready
agent.stopping
agent.stopped

run.started
run.completed
run.failed

model.usage
plugin.loaded
```

Agent 固有イベントも送出可能だが、共通スキーマには押し込まない。

### Sub-agent 補助

Sub-agent の利用判断は Agent が行う。

SDK は次の横断情報を引き継ぐ補助を提供できる。

- correlation_id
- trace context
- deadline
- cancellation
- 共通設定

Sub-agent の選択やルーティングを SDK が自動判断する機能は持たない。

## 3. モデル

Kitsune が OpenAI / Anthropic 等の API を再実装しない。

標準実装では Pydantic AI のモデル抽象化を利用することを想定する。

SDK が提供する価値は以下に限定する。

- 設定ファイルからのモデル選択
- 標準モデルとフォールバックモデルの指定
- 利用量イベントの標準化
- OpenTelemetry との関連付け

高度な Provider 固有機能は利用 Framework に直接渡せるようにする。

## 4. プラグイン

SDK の重要機能。

Agent Application の横断的な機能を後から追加する。

例:

- OpenTelemetry
- Langfuse
- 予算管理
- 調査補助
- MCP 設定補助
- 独自イベント

詳細は [プラグイン機構](plugins.md) を参照。

## 5. Tool を所有しない

Kitsune SDK は Tool Framework を作らない。

```text
Pydantic AI Tool
LangChain Tool
MCP Tool
Application-specific function
```

をそのまま利用する。

AWS、Slack、Jira 等で重複するコードがあれば Utility Library に切り出す。

## 6. 想定 API のモック

```python
from kitsune import Application
from kitsune.plugins import OpenTelemetryPlugin

app = Application(name="sre-agent")
app.use(OpenTelemetryPlugin())

@app.handler()
async def handle(ctx, request):
    # Pydantic AI などは通常の Python として利用する
    return {"message": "result"}

app.run()
```

この API は仕様確定ではなく、責務の確認用である。
