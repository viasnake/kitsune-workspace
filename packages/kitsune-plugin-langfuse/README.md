# kitsune-plugin-langfuse

Langfuse 3 の OpenTelemetry ベースの Client を Kitsune Run Trace に接続します。
無効化時は Langfuse Client を初期化せず、同じ Agent Application コードをそのまま実行できます。
`mask_io` による入出力全体のマスキングと Key Redaction を提供します。Langfuse の
Mask callback は入出力の種別を通知しないため、両者を同じポリシーで扱います。
