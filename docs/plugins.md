# プラグイン

Kitsune Plugin は Agent Application 全体に横断機能を加えます。LLM が呼び出す Tool、通常の Utility Library、MCP Server とは別の仕組みです。

## 公開 Hook

Plugin が実装できる Hook は、構成、起動、停止、Run 開始、Run 終了、Event の六種類です。Model や Tool の前後 Hook は Core にありません。Framework ごとの Usage、Trace、Budget 処理は Pydantic AI または LangChain 連携 Package に置きます。

Plugin は通常の Python Package から Object を Import し、Application 起動前に登録します。

```python
from kitsune_plugin_example import ExamplePlugin

app.use(ExamplePlugin(endpoint="https://plugin.example.invalid"))
```

Plugin 登録は Application 起動前に確定します。依存関係は Topological Sort し、循環または不足を検出すると起動を拒否します。Plugin には SDK 内部の可変 Object を渡しません。

`critical` Plugin の起動失敗は Application 起動失敗です。非 Critical Plugin の観測 Hook が失敗した場合は Run を失敗させず、Plugin 失敗 Event を記録します。

## Budget Plugin

`kitsune-plugin-budget` は Wall Clock、Model Request、Input/Output/Total Token、推定費用、子 Run 数の上限を扱います。

- Soft Limit は `kitsune.budget.soft_limit` Event を出し、`RunContext` に状態を記録します。処理は止めません。
- Hard Limit は次の SDK 管理下 Model 呼び出しまたは子 Run 作成を `BudgetExceeded` で拒否します。すでに実行中の外部通信は強制終了しません。
- `finalization_reserve` は終了処理に必要な利用枠を分離します。
- 取得できない Usage を推測して停止判定に使いません。

Framework Integration の Budget Hook が Model 呼び出しの前後で Plugin に Usage を渡します。

## Langfuse Plugin

`kitsune-plugin-langfuse` は Langfuse の OpenTelemetry 連携を設定し、現在の Kitsune Run Span の下に Model/Tool Span を接続します。入力、出力、Span Attribute の Masking を設定できます。Langfuse は SDK Core の依存ではなく、Plugin を導入していない Agent も同じコードで動きます。

Plugin は通常の Python Package として配布し、`app.use()` で明示的に登録します。中央 Marketplace や Runtime 中の動的な追加・削除はありません。
