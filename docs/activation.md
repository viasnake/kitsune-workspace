# Agent の起動方式

## 1. 起動方式と連携方式を分離する

Kitsune では、Agent が「いつ動くか」と「他 Agent とどう連携するか」を別問題として扱う。

## 2. 起動方式

Workspace が扱う候補。

### 常駐

Workspace / 実行環境とともに起動し、待ち受け続ける。

例:

- Slack 応答 Agent
- 常時監視 Agent

### 要求時

HTTP 等の要求を契機に一回の処理を開始する。

### イベント起動

Alert / Webhook / Queue 等を契機に開始する。

### 定期実行

Cron / Interval。

例:

- Knowledge 更新
- 定期監査

### 手動実行

Workspace Web / CLI から起動する。

## 3. 連携方式

基本的に Agent / SDK 側が所有する。

### 独立

他 Agent と通信しない。

### 同期的委譲

Primary Agent が Sub-agent を呼び、結果を待つ。

### 非同期連携

Agent がイベントを発行し、別 Agent が後から処理する。

この方式が本当に必要になった時点で Workspace の Event 機能を拡張する。

## 4. Swarm の位置付け

Swarm を Kitsune の基本モデルにはしない。

複数 Agent が相互に委譲・協調する構成は、Agent 群の一つの構成パターンである。

Workspace は Swarm 以外にも、独立 Agent、定期 Agent、常駐 Agent を同じように管理できる必要がある。
