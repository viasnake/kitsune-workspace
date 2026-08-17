# ディレクトリ構造

## 1. 方針

Kitsune は一つのモノリシックな AI Agent Framework として実装しない。

リポジトリ上でも、責務を次の単位で分離する。

- `sdk/`: Agent の内側で利用する開発者向け SDK
- `workspace/`: 複数 Agent の運用・管理を担う制御面
- `contracts/`: SDK と Workspace の間で共有する最小限の契約
- `plugins/`: SDK に後から機能を追加する第一級プラグイン
- `utilities/`: Agent / Tool 実装から再利用できる普通のライブラリ
- `examples/`: Kitsune の利用例
- `docs/`: 設計と運用方針
- `mock/`: 設計検証専用の一時的なモック

重要なのは、`plugins/` と `utilities/` を分けることである。

`Plugin` は Kitsune SDK の実行過程へ介入する拡張であり、`Utility` は Kitsune の実行モデルを知らない通常のライブラリである。

また、Agent 固有の Tool や MCP Server をこのリポジトリへ集約しない。Tool は各 Agent Application が所有する。

## 2. 目標構造

実装を開始した後の基本構造は次を想定する。

```text
kitsune/
├── README.md
├── CONTRIBUTING.md
│
├── docs/
│   ├── architecture.md
│   ├── repository-layout.md
│   ├── sdk.md
│   ├── workspace.md
│   ├── plugins.md
│   ├── activation.md
│   ├── observability.md
│   ├── tooling.md
│   └── non-goals.md
│
├── contracts/
│   ├── agent/
│   ├── run/
│   └── event/
│
├── sdk/
│   └── python/
│       ├── pyproject.toml
│       ├── src/
│       │   └── kitsune/
│       └── tests/
│
├── workspace/
│   ├── server/
│   └── web/
│
├── plugins/
│   └── <first-party-plugin>/
│
├── utilities/
│   └── <shared-library>/
│
├── examples/
│   ├── minimal-agent/
│   ├── scheduled-agent/
│   └── managed-agent/
│
└── mock/
    └── ...
```

`<first-party-plugin>` や `<shared-library>` は、実際に二つ以上の Agent で必要になったものだけ追加する。将来必要そうという理由だけで空のパッケージを増やさない。

## 3. `sdk/`

Agent を実装する開発者が直接利用するコードを置く。

標準実装は Python を想定する。

```text
sdk/python/
├── pyproject.toml
├── src/
│   └── kitsune/
│       ├── app/
│       ├── context/
│       ├── events/
│       ├── lifecycle/
│       ├── plugins/
│       ├── telemetry/
│       └── workspace/
└── tests/
```

ここに Pydantic AI や LangChain の Agent 本体を実装しない。

Kitsune SDK が提供するのは、Agent Application の共通骨格、イベント生成、観測性、プラグイン読み込み、Workspace との接続補助などである。

Agent 固有の Prompt、Tool、RAG、MCP 構成、Sub-agent 構成は利用側のリポジトリに置く。

## 4. `workspace/`

複数 Agent を運用するための制御面を置く。

```text
workspace/
├── server/
│   └── Agent 登録、状態、実行要求、履歴、集約処理
└── web/
    └── Agent 一覧、Run、ログ、利用量、状態確認
```

Workspace は Agent の推論や Tool 呼び出しを仲介する通信プロキシではない。

Workspace の主な責務は次である。

- Agent の登録と状態管理
- 起動条件の管理
- 手動実行
- Run / Event / Usage の集約
- ログ・トレースへの導線
- Web 管理画面
- 将来の実行環境アダプター

`server/` と `web/` は別々に配布する必要があるという意味ではない。実装責務を混ぜないための境界である。

## 5. `contracts/`

SDK と Workspace の双方が理解する必要がある、最小限のデータ契約だけを置く。

例えば次である。

```text
AgentDescriptor
RunRecord
RuntimeStatus
KitsuneEvent
UsageRecord
```

ここに Agent の Message、Tool、Prompt、MCP、RAG などのデータモデルを定義しない。

`contracts/` を巨大な Agent Protocol にしないことが重要である。

## 6. `plugins/`

SDK の実行過程へ機能を挿入する第一級プラグインを置く。

例として考えられるものは次であるが、必要になるまで実装しない。

- OpenTelemetry 拡張
- Langfuse 補助
- 予算・利用量制御
- 調査ハーネス

プラグインは、SDK が公開する限定されたフックだけを利用する。

Tool や Utility Library を Plugin と呼ばない。

## 7. `utilities/`

Kitsune の実行モデルから独立した、通常の再利用ライブラリを置く。

例えば複数 Agent で本当に重複した場合にのみ、AWS API の補助、Slack のページング処理、Atlassian の検索補助などを切り出せる。

```text
utilities/
├── aws/
├── slack/
└── atlassian/
```

ただし、これらは例であり最初から作成しない。

Utility は Pydantic AI Tool ではない。必要なら Agent Application 側で Utility を呼ぶ Tool を実装する。

## 8. `examples/`

Kitsune の設計を説明するための小さい利用例を置く。

- `minimal-agent/`: SDK 単体で動く最小 Agent
- `scheduled-agent/`: 定期実行される Agent
- `managed-agent/`: Workspace に登録される Agent

SRE Agent のような実プロジェクトをこのディレクトリへコピーしない。

## 9. `mock/`

現在の設計検証専用である。

実装開始後は `sdk/` と `workspace/` の実コードへ置き換え、役割を終えたモックは削除する。

そのため `mock/` の構造を将来の公開 API として扱わない。

## 10. 各 Agent Application の構造

Kitsune 本体のリポジトリと、Kitsune を使う Agent Application のリポジトリは分ける。

例えば SRE Agent は次のようになる。

```text
sre-agent/
├── pyproject.toml
├── src/
│   └── sre_agent/
│       ├── main.py
│       ├── agents/
│       ├── tools/
│       ├── knowledge/
│       └── domain/
├── config/
├── tests/
└── Dockerfile
```

この中で Kitsune SDK を依存として利用する。

```text
Kitsune repository
    SDK / Workspace / Plugins / Utilities

             ↓ dependency

SRE Agent repository
    Prompt / Tools / RAG / Domain logic
```

これにより Kitsune が各 Agent のドメイン実装を吸収して巨大化することを防ぐ。

## 11. ディレクトリ追加の原則

新しいトップレベルディレクトリは、責務が既存領域と明確に異なる場合だけ追加する。

特に次のような曖昧なディレクトリは作らない。

```text
common/
shared/
misc/
helpers/
services/
platform/
```

共有コードは、性質に応じて `sdk/`、`plugins/`、`utilities/`、`workspace/` のどこに属するかを判断する。
