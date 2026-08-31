# kitsune-integration-pydantic-ai

Pydantic AI の Model 解決、Fallback、Usage 変換、Run Trace 関連付け、MCP 設定、TestModel と
Budget guard を提供します。Provider SDK や Agent Loop は包まず、Pydantic AI の実装を直接利用します。
`run_agent` は Pydantic AI の native instrumentation を有効にし、Model / Tool Span を現在の
Kitsune Run Span の下へ接続します。Prompt、Result、Binary Content は Span に保存しません。
