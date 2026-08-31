# 概念と状態

Kitsune では、論理的な定義、実際に動く個体、一回の処理要求を別の対象として扱います。この区別は API、データベース、Web UI、Event の全体で共通です。

## Agent Definition

一つの論理的な Agent Application に対する望ましい構成です。設定ディレクトリの YAML Manifest が Source of Truth であり、Web UI やデータベースから直接編集しません。データベースには、最後に正常に読み込んだ Snapshot と Hash が残ります。

## Agent Descriptor

起動した SDK が実際の能力を報告した記録です。Application と SDK の Version、Framework、Build Revision、Handler と入出力 JSON Schema、Plugin、起動時刻を持ちます。Definition が望ましい構成を示すのに対し、Descriptor は稼働中の個体が提供している内容を示します。

## Runtime Instance

一つの Process、Container、または外部で稼働する Agent endpoint です。一つの Agent Definition から複数の Runtime Instance が生まれます。

```text
pending -> starting -> ready -> stopping -> stopped
                     |  |
                     |  +-> unhealthy -> ready | failed | lost
                     +----> failed
```

`lost` は Heartbeat が設定した猶予を超えて途絶えた状態です。`failed` は起動失敗、異常終了、または Restart Policy の上限に達した状態です。

## Handler

Workspace や Scheduler から呼べる、名前付きで型付けされた Agent Application の入口です。Handler は LLM が選択する Tool ではありません。入力と出力の Pydantic Model から JSON Schema を生成します。

## Run

一回の処理要求と実行を表します。Run は Runtime Instance と別に保存され、再起動や Ephemeral 実行でも実行履歴を失いません。

```text
created -> queued -> dispatching -> running -> succeeded
                                      |  |  |-> failed
                                      |  +----> cancelled
                                      +-------> timed_out
```

Run は一つの終端状態だけに到達します。親 Run、相関 ID、Trace ID により、子 Run や別 Agent への明示的な委譲を追跡できます。

## Trigger と Runtime Mode

Trigger は Run が作られた原因です。

| 値 | 原因 |
| --- | --- |
| `on_demand` | Web UI または API の手動実行 |
| `schedule` | Cron Schedule |
| `webhook` | 署名を検証した外部 Webhook |
| `self` | Slack など Agent 自身が所有する入口 |
| `child` | 親 Run から開始した子処理 |

Runtime Mode は Agent Process の生存方式です。`resident` は複数 Run を処理する常駐個体、`ephemeral` は Run ごとに起動して終了する個体です。常駐 Slack Agent は `resident` と `self`、定期処理は `ephemeral` と `schedule` のように組み合わせます。

## Event と Usage

Kitsune Event は一意な Event ID を持つ不変の運用記録です。標準 Event は `kitsune.` 名前空間を使い、Agent 固有 Event は `sre.` などの固有名前空間を使います。Workspace は固有 Event の Payload を解釈せず保存できます。

Usage は Provider、Model、Request 数、Token、推定費用を取得できた範囲だけ記録します。取得できない値を推測せず、料金表を Core に固定しません。
