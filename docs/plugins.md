# プラグイン機構

## 1. 目的

Kitsune SDK の機能を Core へ追加し続けるのではなく、横断機能を後付けできるようにする。

プラグインは Tool とは異なる。

| 種類 | 役割 |
|---|---|
| Plugin | Agent Application 全体の振る舞いを拡張する |
| Tool | LLM が呼び出す能力 |
| Utility | 普通のコードから再利用するライブラリ |
| MCP | 外部から Tool / Resource 等を提供する仕組み |

## 2. 安定した介入点

プラグインが自由に内部状態を書き換える方式にはしない。

最初に検討する介入点:

```text
application.starting
application.started
application.stopping
application.stopped

run.starting
run.completed
run.failed

model.before
model.after

tool.before
tool.after

event.emitted
```

必要性が確認されてから追加する。

## 3. 例

### OpenTelemetry プラグイン

Agent 実行、Model 呼び出し、Tool 呼び出しなどを Trace に関連付ける。

### Langfuse プラグイン

OpenTelemetry または Langfuse SDK と接続する補助。

Langfuse を Core 依存にしない。

### 予算管理プラグイン

- Model 利用量
- 推定料金
- Tool 呼び出し数
- 経過時間

等を観測し、Soft / Hard Limit の方針を追加できる。

### 調査プラグイン

将来的に Kitsune 由来の調査ハーネスを実験する場所。

Core Agent Loop を調査専用に固定しない。

## 4. Python パッケージとして配布

中央プラグイン市場は作らない。

```bash
pip install kitsune-plugin-example
```

```python
app.use(ExamplePlugin())
```

程度を基本とする。

## 5. 依存関係

必要なら以下のメタデータを持てるようにする。

```text
name
version
requires
optional_requires
```

ただし複雑な依存解決システムを自作しない。
