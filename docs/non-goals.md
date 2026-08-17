# 非目標と判断基準

## 非目標

現時点で Kitsune が作らないもの。

- 独自 LLM API
- 独自 Tool Protocol
- MCP 互換実装
- 汎用 Workflow Engine
- Agent の reasoning engine
- RAG Framework
- Vector DB abstraction
- Agent Swarm の強制
- Agent 同士の自動ルーティング
- 全外部通信を仲介する Proxy
- Plugin Marketplace
- 全クラウド向けのデプロイ基盤

## 判断基準

### SDK に入れる

Agent を複数作ったとき、Agent プロセス内で繰り返し実装される横断処理。

例:

- lifecycle
- event emission
- trace context
- plugin host

### Workspace に入れる

複数 Agent を横断して管理・観測する必要があるもの。

例:

- Agent registry
- run history
- state
- schedules
- manual invocation
- Web UI

### Utility に入れる

AI Agent に限らず再利用可能な通常ライブラリ。

### Agent に残す

用途固有の知能・外部サービス利用。

- Prompt
- Tool
- MCP
- RAG
- Domain logic
- Sub-agent 構成
