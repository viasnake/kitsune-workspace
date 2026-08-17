# Contributing

このリポジトリの現在の目的は、コード量を増やすことではなく責務を明確にすることです。

変更時には次を確認してください。

1. その機能は SDK、Workspace、Utility、Agent Application のどこに属するか。
2. 既存の Pydantic AI / LangChain / MCP / OpenTelemetry の機能を重複実装していないか。
3. 将来の可能性だけを理由に Core の抽象化を増やしていないか。
4. Workspace を Agent の必須通信経路にしていないか。
5. Agent 群の協調方法を特定の方式に固定していないか。

設計変更はまず `docs/` を更新し、モックで境界を確認してから実装へ進めます。
