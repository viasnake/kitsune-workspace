# kitsune-plugin-budget

Run ごとの実測 Wall Clock、Model Request、Token、推定費用、子 Run 数に Soft / Hard Limit を
適用します。取得できない Usage 値を推測せず、Hard Limit は新しい SDK 管理下の Model 呼び出し
または子 Run の開始時に型付き例外として通知します。
