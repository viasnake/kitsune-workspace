# kitsune-sdk

Agent Application のプロセス内で Handler 登録、Run Context、ライフサイクル、
構造化イベント、OpenTelemetry、耐久 Event Outbox、Plugin Host、型付き
Workspace Client を提供します。

Workspace Client は Agent 起点の Run と Ephemeral Run に共有契約
`AgentRunBegin`、`AgentRunAssignment`、`AgentRunAcknowledgement` を使用します。
Resident Control API も Run の受付に `AgentRunAssignment` を使用します。

Resident Control API の Health は認証なしで取得できます。`KITSUNE_AGENT_TOKEN` を
設定した Agent では Descriptor、Run の開始、Cancel に同じ Bearer 認証が必要です。
Descriptor は JSON Schema の Property 名を保ったまま Secret を除去します。Token のない
Standalone Mode では、Control API を Loopback にのみ Bind できます。Request の Host は
設定した Port 上の Loopback または `localhost` に限定され、`Origin` Header がある場合は
Bind Origin との完全一致が必要です。

## Model 呼び出しの事前確認

独自の Provider 統合では、各 Model 呼び出しの直前に
`RunContext.check_model_call()` を呼びます。戻り値の
`ModelCallAdmission` は Usage Event の永続化容量を確保しています。呼び出しが失敗する、
または Usage を生成しない場合は `release()` で容量を返します。Usage を取得できた場合は
`RunContext.record_usage()` に `UsageRecord` を渡します。

```python
admission = await context.check_model_call()
try:
    response = await provider_call()
except BaseException:
    admission.release()
    raise

if response.usage is None:
    admission.release()
else:
    await context.record_usage(response.usage)
```

公式の Pydantic AI および LangChain 統合は、この事前確認と解放を自動で行います。

## Ephemeral Run の Event 復旧

外部オーケストレータが起動する Ephemeral Run では、`KITSUNE_OUTBOX_PATH`
をプロセスの再起動後も残るボリューム上に設定します。停止時に
`KITSUNE_SHUTDOWN_GRACE_SECONDS` 以内で Outbox が空にならない場合、
`kitsune agent once` はキューを残したまま一時的失敗を示す終了コード `75`
を返します。

Workspace の復旧後、同じ `KITSUNE_OUTBOX_PATH` と
`KITSUNE_OUTBOX_DRAIN_ONLY=1` を設定し、同じ Application 参照で
`kitsune agent once <module:attribute>` を起動します。この復旧モードは、残った
Event の送信以外を実行しません。Agent 登録、Heartbeat、Run 受領確認、
Handler、ライフサイクル Event は実行または新規作成されません。Outbox が
空になった場合だけ `0` を返し、Event が残った場合は再び `75` を返します。
外部オーケストレータは `0` になるまで復旧プロセスを再起動します。
