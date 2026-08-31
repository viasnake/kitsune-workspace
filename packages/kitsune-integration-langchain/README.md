# kitsune-integration-langchain

LangChain Runnable / Agent の非同期実行、Callback Usage、Kitsune Run Trace、Budget guard、
Streaming Event の進行 Event 変換を提供します。Tool、State、Memory、Middleware は再実装しません。
Native callback の Model / Tool lifecycle を OpenTelemetry Span に変換し、現在の
Kitsune Run Span の下へ接続します。Prompt、Result、Tool 引数は Span に保存しません。
