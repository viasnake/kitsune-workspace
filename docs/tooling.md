# Utility / Tool / MCP の境界

## 1. Tool を Kitsune の共通 API にしない

AWS、Slack、Jira、Akamai 等を一つの Kitsune Tool 抽象に統一しない。

理由:

- Provider 固有機能が必ず必要になる
- Tool の入力・出力は Agent ごとに望ましい粒度が異なる
- Pydantic AI / LangChain / MCP が既に Tool 機構を持つ
- Kitsune が別の互換層を維持するコストが高い

## 2. Utility Library

再利用価値がある通常コードは Utility とする。

例:

```text
kitsune-util-aws
├── session helper
├── pagination
├── ARN helper
└── retry helper

kitsune-util-atlassian
├── pagination
├── auth helper
└── common response parsing
```

Agent Tool はこれらを組み合わせて各 Agent が作る。

## 3. MCP

MCP Server は既存仕様・既存ライブラリを利用する。

Kitsune SDK は、

- MCP 設定を読み込みやすくする
- 観測情報を関連付ける
- プラグインとして接続を補助する

程度に留める。

独自 MCP 互換層は作らない。

## 4. Preset

開発速度向上のため、Tool 自体ではなく「よく使う構成」を Preset として提供する案は有効。

例:

```text
operational preset
├── OpenTelemetry
├── model usage logging
├── default error handling
└── recommended lifecycle
```

Preset は設定の集合であり、Agent の能力を制限する独自 Runtime にはしない。
