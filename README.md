# Claude Code Telegram Bot

> **[日本語](#日本語)** | **[中文](#中文)**

---

## 日本語

Telegram 経由で Claude Code CLI を操作するボットです。

### 用途

- Telegram でリクエストを送信
- Claude Code がバックグラウンドで開発タスクを実行
- 長時間タスクの状態確認・停滞検知に対応

### 機能

- ユーザーごとに同時実行タスクは1つまで
- タスクはバックグラウンド実行（Telegram ポーリングをブロックしない）
- タスク状態は `tasks/` ディレクトリにファイル永続化
- 実行健全性の判定：
  - `active`: 最近アクティビティあり
  - `quiet`: しばらくアクティビティなし
  - `stalled`: 長時間アクティビティなし（停滞の疑い）
- 停滞検知時の通知は1回のみ（通知スパム防止）

### Telegram コマンド

- `/start` — ウェルカムメッセージを表示
- `/help` — 利用可能なコマンド一覧を表示
- `/status` — 現在のタスク状態を確認（実行時間・進捗・健全性）
- `/logs [n]` — 最近 n 件のタスクログを表示（デフォルト 8、最大 50）
- `/cancel` — 実行中のタスクをキャンセル
- `/reset` — Claude セッションをリセット（先にタスクをキャンセルする必要あり）
- `/model [alias]` — モデルの確認・切替（略称：sonnet / opus / haiku）
- `/models` — 利用可能なモデル一覧（InlineKeyboard で選択）
- `/workdir [path]` — 作業ディレクトリの確認・切替（許可されたパスプレフィックス内のみ）
- `/ls` — プロジェクトディレクトリを一覧表示（タップで切替）
- `/history [n]` — 最近 n 件のタスク履歴を表示（デフォルト 5、最大 20）

テキストメッセージを送信すると、新しいタスクとして Claude Code に送られます。

### ステータス

メインステータス：

- `queued`: キュー待ち
- `running`: 実行中
- `done`: 完了
- `error`: エラー
- `cancelled`: キャンセル済み
- `timeout`: タイムアウト

健全性（`running` 時のサブステータス）：

- `active`: 最近アクティビティあり
- `quiet`: しばらくアクティビティなし
- `stalled`: 長時間アクティビティなし（停滞の疑い、確定ではない）

### ローカルファイル

- `bot.py` — メインプログラム
- `claudecode-chatbot.service` — systemd サービス定義
- `.env.example` — 環境変数テンプレート
- `sessions.json` — Claude セッション永続化
- `tasks/` — タスク状態とログ

### 環境変数

必須：

- `TELEGRAM_BOT_TOKEN`

任意：

- `ALLOWED_USER_IDS` — 許可するユーザーID（カンマ区切り）
- `CLAUDE_CMD` — Claude Code CLI パス
- `CLAUDE_TIMEOUT` — タスクの最大実行時間（秒、デフォルト 3600）
- `MAX_CONCURRENT_TASKS` — 最大同時実行タスク数（デフォルト 2）
- `TASK_QUIET_AFTER` — quiet 判定閾値（秒、デフォルト 180）
- `TASK_STALLED_AFTER` — stalled 判定閾値（秒、デフォルト 480）
- `STALL_CHECK_INTERVAL` — stalled チェック間隔（秒、デフォルト 60）
- `TASK_LOG_RETENTION_DAYS` — ログ保持日数（デフォルト 7）
- `ALLOWED_WORKDIR_PREFIX` — 許可する作業ディレクトリプレフィックス（デフォルト `/home/sikim/project`）

詳細は `.env.example` を参照。

### 起動・再起動

Python コードや `.env` のみ変更した場合：

```bash
sudo systemctl restart claudecode-chatbot.service
```

状態確認：

```bash
sudo systemctl status claudecode-chatbot.service --no-pager
```

ログ確認：

```bash
sudo journalctl -u claudecode-chatbot.service -n 100 --no-pager
```

service ファイル自体を変更した場合：

```bash
sudo systemctl daemon-reload
sudo systemctl restart claudecode-chatbot.service
```

### 設計方針

以下は意図的に実装していません：

- 固定間隔の定期レポート
- 「分析 / コーディング / テスト / デプロイ」等のプロジェクトフェーズ表示
- プロジェクト md ファイルの内容を実行状態として扱うこと

理由：

- Telegram は「実行状態の可視化」が目的であり、プロジェクト管理ではない
- 固定ハートビートはノイズと偽アクティビティを生む
- 実際の実行イベントに基づかないプロジェクトフェーズはユーザーを誤解させる

### 既知の制限

- Claude CLI は最終 JSON を一括返回する方式であり、ストリーミングイベントではない
- そのため「最近の進捗」は Claude 内部の詳細ステップではなく、bot 外層のタスク実行状態を反映
- より細かい粒度の進捗が必要な場合は、Claude の出力解析または hook メカニズムの導入が必要

---

## 中文

通过 Telegram 驱动 Claude Code CLI 的机器人。

### 用途

- 在 Telegram 里直接发送需求
- Claude Code 在后台执行开发任务
- 支持长时间运行的任务状态查看和卡住检测

### 功能

- 每个用户同一时间最多运行一个任务
- 任务在后台执行，不阻塞 Telegram 轮询
- 任务状态持久化到本地 `tasks/` 目录
- 运行健康度判断：
  - `active`: 最近有活动
  - `quiet`: 一段时间没有新活动
  - `stalled`: 长时间没有新活动，疑似卡住
- 长时间无活动时，只提醒一次（避免通知轰炸）

### Telegram 命令

- `/start` — 显示欢迎消息
- `/help` — 显示所有可用命令
- `/status` — 查看当前任务状态（运行时长、进展、健康度）
- `/logs [n]` — 查看最近 n 条任务日志（默认 8，最多 50）
- `/cancel` — 取消当前正在运行的任务
- `/reset` — 重置 Claude 会话（需先取消任务）
- `/model [alias]` — 查看或切换模型（可用简写：sonnet / opus / haiku）
- `/models` — 显示可用模型列表（InlineKeyboard 选择）
- `/workdir [path]` — 查看或切换工作目录（限制在允许的路径前缀下）
- `/ls` — 浏览项目目录（点击切换工作目录）
- `/history [n]` — 查看最近 n 个任务历史（默认 5，最多 20）

直接发送文本消息即可创建新任务。

### 状态说明

主状态：

- `queued`: 排队中
- `running`: 执行中
- `done`: 已完成
- `error`: 已出错
- `cancelled`: 已取消
- `timeout`: 已超时

健康度（`running` 下的子状态）：

- `active`: 最近有活动
- `quiet`: 一段时间没有新活动
- `stalled`: 长时间没有新活动，疑似卡住（不代表已确定停止）

### 本地文件

- `bot.py` — 主程序
- `claudecode-chatbot.service` — systemd 服务定义
- `.env.example` — 环境变量模板
- `sessions.json` — Claude 会话持久化
- `tasks/` — 任务状态和日志

### 环境变量

必填：

- `TELEGRAM_BOT_TOKEN`

可选：

- `ALLOWED_USER_IDS` — 允许的用户 ID（逗号分隔）
- `CLAUDE_CMD` — Claude Code CLI 路径
- `CLAUDE_TIMEOUT` — 单个任务最长执行时间（秒，默认 3600）
- `MAX_CONCURRENT_TASKS` — 同时最多运行多少个任务（默认 2）
- `TASK_QUIET_AFTER` — quiet 判定阈值（秒，默认 180）
- `TASK_STALLED_AFTER` — stalled 判定阈值（秒，默认 480）
- `STALL_CHECK_INTERVAL` — stalled 检查间隔（秒，默认 60）
- `TASK_LOG_RETENTION_DAYS` — 日志保留天数（默认 7）
- `ALLOWED_WORKDIR_PREFIX` — 允许的工作目录前缀（默认 `/home/sikim/project`）

详见 `.env.example`。

### 启动与重启

仅修改 Python 代码或 `.env` 时：

```bash
sudo systemctl restart claudecode-chatbot.service
```

查看状态：

```bash
sudo systemctl status claudecode-chatbot.service --no-pager
```

查看日志：

```bash
sudo journalctl -u claudecode-chatbot.service -n 100 --no-pager
```

修改了 service 文件本身时：

```bash
sudo systemctl daemon-reload
sudo systemctl restart claudecode-chatbot.service
```

### 设计原则

以下是刻意不做的事情：

- 不做固定频率定时汇报
- 不做"分析 / 编码 / 测试 / 部署"这类项目阶段展示
- 不把项目 md 文档内容当运行状态

原因：

- Telegram 主要解决"运行态可见性"，不是项目管理
- 固定心跳容易制造噪音和假活跃
- 项目阶段如果不是从真实运行事件得到，很容易误导用户

### 已知限制

- Claude CLI 当前仍然是一次性返回最终 JSON，不是流式事件接口
- 因此"最近进展"更多反映 bot 外层任务运行状态，而不是 Claude 内部精细步骤
- 如果后续需要更细颗粒度进展，需要引入 Claude 输出解析或 hook 机制
