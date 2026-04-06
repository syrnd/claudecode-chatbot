# Claude Code Telegram Bot

这是一个通过 Telegram 驱动 Claude Code CLI 的机器人。

用途：

- 在 Telegram 里直接发送需求
- Claude Code 根据需求执行开发任务
- 支持较长时间运行的后台任务
- 在 Telegram 中查看当前任务状态、最近进展和疑似卡住提醒

## 当前能力

- 每个用户同一时间最多运行一个任务
- 任务在后台执行，不阻塞 Telegram 轮询
- 当前任务状态会持久化到本地 `tasks/` 目录
- 支持运行健康度判断：
  - `active`: 最近有活动
  - `quiet`: 一段时间没有新活动
  - `stalled`: 长时间没有新活动，疑似卡住
- 长时间无活动时，只提醒一次“疑似卡住”

## Telegram 命令

- `/start`
  - 显示欢迎消息
- `/reset`
  - 重置当前 Claude 会话
  - 如果任务仍在运行，需先 `/cancel`
- `/status`
  - 查看当前任务真实状态
  - 返回任务状态、运行时长、最近进展、最近更新时间、健康度
- `/logs`
  - 查看最近几条任务日志
- `/cancel`
  - 取消当前正在运行的任务

普通文本消息会作为新任务发送给 Claude Code。

## 状态说明

主状态：

- `queued`: 排队中
- `running`: 执行中
- `done`: 已完成
- `error`: 已出错
- `cancelled`: 已取消
- `timeout`: 已超时

健康度：

- `active`: 最近有活动
- `quiet`: 一段时间没有新活动
- `stalled`: 长时间没有新活动，疑似卡住

说明：

- `stalled` 不是主状态，而是 `running` 下的健康判断
- “疑似卡住”表示长时间没有新的执行反馈，不代表已经确定停止

## 本地文件

- [`bot.py`](/srv/claudecode-chatbot/bot.py)
  - 主程序
- [`claudecode-chatbot.service`](/srv/claudecode-chatbot/claudecode-chatbot.service)
  - systemd service
- [`.env.example`](/srv/claudecode-chatbot/.env.example)
  - 环境变量示例
- `/srv/claudecode-chatbot/sessions.json`
  - Claude 会话持久化
- `/srv/claudecode-chatbot/tasks/`
  - 当前任务状态和日志

## 环境变量

必填：

- `TELEGRAM_BOT_TOKEN`

可选：

- `ALLOWED_USER_IDS`
- `CLAUDE_CMD`
- `CLAUDE_TIMEOUT`
- `MAX_CONCURRENT_TASKS`
- `TASK_QUIET_AFTER`
- `TASK_STALLED_AFTER`
- `STALL_CHECK_INTERVAL`

参考 [`.env.example`](/srv/claudecode-chatbot/.env.example)。

## 启动与重启

如果只改了 Python 代码或 `.env`，直接重启服务即可：

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

如果修改了 service 文件本身，再执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart claudecode-chatbot.service
```

## 设计原则

当前实现刻意不做这些事情：

- 不做固定频率定时汇报
- 不做“分析 / 编码 / 测试 / 部署”这类项目阶段展示
- 不把项目 `md` 文档内容当运行状态

原因：

- Telegram 主要解决“运行态可见性”，不是项目管理
- 固定心跳容易制造噪音和假活跃
- 项目阶段如果不是从真实运行事件得到，很容易误导用户

## 已知限制

- Claude CLI 当前仍然是一次性返回最终 JSON，不是流式事件接口
- 因此“最近进展”更多反映 bot 外层任务运行状态，而不是 Claude 内部精细步骤
- 如果后续需要更细颗粒度进展，需要引入 Claude 输出解析或 hook 机制
