# 共有契約

`kitsune-contracts` は SDK と Workspace が共有する Pydantic Model、状態遷移、JSON Schema 生成を提供します。Python Model を正とし、同じ契約を手書きの TypeScript や JSON Schema として重複管理しません。Workspace の OpenAPI と Web Client はこの Model から生成されます。

## 契約の識別

Agent Manifest は次の識別子を持ちます。

```yaml
schema: kitsune.agent
revision: 1
```

`revision` は契約互換性を判定する番号であり、製品の段階を表しません。未知の Schema、未対応 Revision、余分な Field、無効な状態や参照は Validation Error になります。

## 主な Model

- `AgentManifest`: Metadata、Runtime、Invocation、Trigger、Observability、Security、Payload 保存方針
- `AgentDescriptor`: 起動した SDK が報告する Version、Framework、Handler、Plugin、Build 情報
- `RuntimeConfiguration`: Adapter、Mode、Desired State と Process、Container、External の宣言設定
- `InvocationConfiguration`: Agent 全体の受付制限と、Handler ごとの型付き上書き
- `RunRecord`: 入出力、親子関係、相関、時刻、状態、Error、Usage
- `KitsuneEvent`: Event ID、発生時刻、Agent、Runtime、Run、Severity、任意 Payload
- `UsageRecord`: 取得できた Provider/Model 利用量
- `AgentRegistration`、`AgentHeartbeat`: Resident SDK から Workspace へ送る登録・生存契約
- `EventBatch`: Managed SDK から Workspace へ送る Event 契約
- `AgentRunBegin`、`AgentRunAssignment`、`AgentRunAcknowledgement`: Self/Child Run の開始と Ephemeral Run の受渡し契約

Enum は文字列として JSON に出力されます。`RunRecord` は共有の遷移表で検証し、不正な逆行や終端状態からの変更を拒否します。Runtime Instance の永続状態は Workspace が管理します。

## Schema の出力

```bash
kitsune manifest schema > agent-manifest.schema.json
kitsune manifest validate config/examples/agents/managed-resident-agent.yaml
```

OpenAPI は起動中の Workspace が `/openapi.json` で公開します。Frontend Client の生成元を更新した場合は Contract Drift Check を実行し、生成差分が残っていないことを確認します。

## Event 配送

SDK は標準 Event を SQLite Outbox に先に保存し、Batch API へ少なくとも一回配送します。Run を受け付ける前に開始・終端 Event、Model 呼び出し前に Usage Event の容量を予約するため、上限到達後に必須 Event だけを失うことはありません。Workspace は `event_id` に Unique 制約を持ち、同じ Event の再送を成功扱いにして重複保存しません。Heartbeat は履歴保証が不要なため Outbox に入れません。

Agent 固有 Event の `payload` は JSON Object であれば保存できます。Workspace が固有 Schema を知る必要はありません。入力、出力、Event Payload には Workspace の Size Limit を適用し、Webhook は Trigger ごとの `max_request_bytes` も適用します。
