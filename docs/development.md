# 開発

## 必要な環境

- Python 3.12 以上
- uv
- Node.js 24 以上
- pnpm 11
- Docker Engine と Compose Plugin

`mise` は必須ではありません。利用する場合も、Repository の標準コマンドは `uv`、`pnpm`、`docker compose` のままです。

## 依存関係を入れる

```bash
uv sync --all-packages --all-groups
pnpm install --frozen-lockfile
```

Python は uv Workspace と単一の `uv.lock`、Frontend は pnpm Workspace と単一の `pnpm-lock.yaml` を使います。依存を変更したら両 Lockfile を更新し、Source Tree と同じ Commit に含めます。

## Repository の構成

```text
packages/             Contracts、SDK、Framework Integration、Plugin
services/workspace/   FastAPI、SQLAlchemy、Runtime、Scheduler、CLI
apps/workspace-web/   React の運用 UI
examples/             Standalone、Resident、Ephemeral、External Agent
config/examples/      検証できる Agent Manifest
deploy/               Container、Compose、Collector、設定例
tests/                Package を横断する Integration と E2E
```

`common/`、`shared/`、`misc/`、`helpers/`、`platform/` のように所有境界が不明な Directory は作りません。重複が実際に生じるまでは Utility Package も作りません。

## 検証

狭い Test から始め、変更範囲に応じて全体へ広げます。

```bash
uv run pytest packages/kitsune-sdk/tests
uv run pytest services/workspace/tests
pnpm --dir apps/workspace-web test

make check
make artifacts
```

`make check` は Ruff、Pyright、pytest、ESLint、TypeScript、Vitest、Frontend Build を実行します。`make artifacts` は全 Python wheel と sdist を Build し、Metadata、License、型情報、Workspace Migration の同梱内容を検査します。Docker を含む検証は次です。

```bash
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up --detach
uv run pytest tests/e2e
KITSUNE_COMPOSE_E2E=1 KITSUNE_E2E_URL=http://127.0.0.1:8080 \
  pnpm --dir apps/workspace-web test:e2e --grep "Docker Compose live"
docker compose -f deploy/docker-compose.yml down
```

CI は SQLite と PostgreSQL、Process/Docker/External Adapter、Contract Drift、Frontend、Compose E2E、Image Build、Secret Scan、Dependency Audit を別 Job でも確認します。OpenAI、Anthropic、Dynatrace、Langfuse には接続せず、Fake Provider、Test Model、Mock OTLP を使います。

## OpenAPI Client

Workspace の Pydantic Model と FastAPI Route が OpenAPI の Source of Truth です。Frontend Client を更新する時は Backend の OpenAPI を生成し、Client Generator を実行します。

```bash
uv run python -m kitsune_workspace.openapi --output services/workspace/openapi.json
pnpm --dir apps/workspace-web generate:api
git diff --exit-code -- services/workspace/openapi.json apps/workspace-web/src/api/openapi.generated.ts
```

生成後に手書き修正しません。API を変更した Pull Request には Client の差分と UI/Test の更新を含めます。

## Database

Migration は `services/workspace/migrations/versions` に追加し、SQLite と PostgreSQL の両方で Upgrade を検証します。配布経路の確認には `kitsune workspace migrate --config <workspace.toml>` を実行します。SQLite の空の開発用 Database は起動時に初期 Schema を作成します。PostgreSQL は配布 Package の現在 Revision が適用されていなければ起動しません。識別子と参照整合性は Database Schema でも守り、状態遷移、所有関係、Payload の制約は Service で検証します。

## 文書

日本語の公開文書は現在の操作と制約だけを説明します。計画、過去の判断、互換性 History を Source of Truth にしません。公開 API には型注釈と Docstring を付けます。Command、Path、Identifier は実装にある英語名を使います。

## Commit と Pull Request

一つの変更意図ごとに Conventional Commits を使います。Branch は短命にし、Default Branch へ Pull Request で統合します。Pull Request Template の Problem、Changes、Impact を埋め、実行できなかった外部確認があれば Impact に残します。
