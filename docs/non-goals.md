# Kitsune が実装しないもの

Kitsune は Agent Application の共通運用を標準化します。Agent の推論能力や外部 Service を置き換える Framework ではありません。

Core には次を実装しません。

- 独自 LLM API または Model Provider API
- 独自 Tool Protocol または MCP 互換実装
- Vector Database 抽象化、RAG Framework、Prompt 管理 Service
- 汎用 Workflow Engine、Swarm 実行 Engine、自動の意味的 Routing
- 任意 Bash 実行基盤、承認付きの自動 Infrastructure 変更基盤
- 全外部通信を中継する Credential Proxy
- Kafka などの Message 基盤、汎用 Log/Artifact Store
- Plugin Marketplace、Kubernetes Operator、全 Cloud Provider の配備管理
- 調査計画、仮説 Graph、Evidence Graph を強制する調査 Harness

Agent は AWS、Slack、Jira、MCP Server、Database へ直接接続できます。どの Tool、Sub-agent、Provider、Memory、RAG を使うかは Agent Application または既存 Framework が決めます。

複数 Agent で実際に重複した通常処理は、Kitsune の Lifecycle や Plugin API に依存しない Utility Library として切り出せます。将来必要になるという理由だけで空の抽象化は作りません。

調査 Harness を Core に含めないのは機能不足ではありません。SRE、Security、Support では必要な調査手順が異なるため、固定すると Workspace が業務固有の Workflow Engine になります。必要な Harness は公開 Plugin Hook、子 Run、Budget、Event を使う別 Package または Agent Application として実装します。
