import asyncio
import contextlib
import json
import logging
import os
import pty
import shlex
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = set(
    int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()
)

# claude CLI 路径和超时（默认10分钟）
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "/home/sikim/.npm-global/bin/claude")
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "3600"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))
TASK_QUIET_AFTER = int(os.getenv("TASK_QUIET_AFTER", "180"))
TASK_STALLED_AFTER = int(os.getenv("TASK_STALLED_AFTER", "480"))
STALL_CHECK_INTERVAL = int(os.getenv("STALL_CHECK_INTERVAL", "60"))
TASK_LOG_RETENTION_DAYS = int(os.getenv("TASK_LOG_RETENTION_DAYS", "7"))
ALLOWED_WORKDIR_PREFIX = os.getenv("ALLOWED_WORKDIR_PREFIX", "/home/sikim/project")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Session 持久化文件
SESSIONS_FILE = "/srv/claudecode-chatbot/sessions.json"
USER_SETTINGS_FILE = "/srv/claudecode-chatbot/user_settings.json"
TASKS_DIR = Path("/srv/claudecode-chatbot/tasks")
sessions_lock = Lock()
task_state_lock = Lock()
user_settings_lock = Lock()

AVAILABLE_MODELS = [
    ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-opus-4-6", "Opus 4.6"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5"),
]
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL = "claude-sonnet-4-6"
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)


@dataclass
class RunningTask:
    task: asyncio.Task
    process: asyncio.subprocess.Process | None
    started_at: float
    prompt_preview: str
    status_message_chat_id: int
    status_message_id: int
    task_id: str
    stalled_notified: bool = False


active_tasks: dict[int, RunningTask] = {}


def load_sessions() -> dict[int, str]:
    try:
        if os.path.exists(SESSIONS_FILE):
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        logger.exception("load_sessions error")
    return {}

def load_user_settings() -> dict[int, dict]:
    try:
        if os.path.exists(USER_SETTINGS_FILE):
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
    except Exception:
        logger.exception("load_user_settings error")
    return {}


def save_user_settings(settings: dict[int, dict]):
    try:
        path = Path(USER_SETTINGS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with user_settings_lock:
            with tempfile.NamedTemporaryFile(
                "w", dir=path.parent, delete=False, encoding="utf-8"
            ) as tmp:
                json.dump({str(k): v for k, v in settings.items()}, tmp, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
                temp_name = tmp.name
            os.replace(temp_name, path)
    except Exception:
        logger.exception("save_user_settings error")


def get_user_model(user_id: int) -> str:
    return user_settings.get(user_id, {}).get("model", DEFAULT_MODEL)


def get_user_workdir(user_id: int) -> str | None:
    return user_settings.get(user_id, {}).get("workdir")


async def set_user_setting(user_id: int, key: str, value):
    if user_id not in user_settings:
        user_settings[user_id] = {}
    user_settings[user_id][key] = value
    await asyncio.to_thread(save_user_settings, user_settings)


def save_sessions(sessions: dict[int, str]):
    try:
        sessions_path = Path(SESSIONS_FILE)
        sessions_path.parent.mkdir(parents=True, exist_ok=True)
        with sessions_lock:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=sessions_path.parent,
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump({str(k): v for k, v in sessions.items()}, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
                temp_name = tmp.name
            os.replace(temp_name, sessions_path)
    except Exception:
        logger.exception("save_sessions error")


def ensure_tasks_dir():
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_old_logs():
    if not TASKS_DIR.exists():
        return
    cutoff = now_ts() - TASK_LOG_RETENTION_DAYS * 86400
    for log_file in TASKS_DIR.glob("*.log"):
        try:
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
                logger.info("deleted old log %s", log_file.name)
        except Exception:
            logger.exception("cleanup_old_logs error file=%s", log_file.name)


def current_task_file(user_id: int) -> Path:
    return TASKS_DIR / f"user-{user_id}-current.json"


def history_file(user_id: int) -> Path:
    return TASKS_DIR / f"user-{user_id}-history.json"


def load_history(user_id: int, limit: int = 20) -> list[dict]:
    try:
        path = history_file(user_id)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            return entries[-limit:]
    except Exception:
        logger.exception("load_history error user=%d", user_id)
    return []


def append_history(state: dict):
    try:
        ensure_tasks_dir()
        user_id = int(state["user_id"])
        path = history_file(user_id)
        entries = []
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)
        entry = {
            "task_id": state.get("task_id"),
            "prompt_preview": state.get("prompt_preview", ""),
            "status": state.get("status"),
            "created_at": state.get("created_at"),
            "finished_at": state.get("finished_at"),
            "error": state.get("error"),
        }
        entries.append(entry)
        # 只保留最近 50 条
        entries = entries[-50:]
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(entries, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.replace(temp_name, path)
    except Exception:
        logger.exception("append_history error")


def task_log_file(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.log"


def human_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟{sec}秒"
    return f"{sec}秒"


def now_ts() -> float:
    return time.time()


def load_task_state(user_id: int) -> dict | None:
    try:
        path = current_task_file(user_id)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception("load_task_state error user=%d", user_id)
    return None


def save_task_state(state: dict):
    try:
        ensure_tasks_dir()
        path = current_task_file(int(state["user_id"]))
        with task_state_lock:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=path.parent,
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump(state, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                temp_name = tmp.name
            os.replace(temp_name, path)
    except Exception:
        logger.exception("save_task_state error task_id=%s", state.get("task_id"))


def append_task_log(task_id: str, message: str):
    try:
        ensure_tasks_dir()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts()))
        with open(task_log_file(task_id), "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        logger.exception("append_task_log error task_id=%s", task_id)


async def update_task_state(
    user_id: int,
    *,
    status: str | None = None,
    last_progress: str | None = None,
    error: str | None = None,
    pid: int | None = None,
    session_id: str | None = None,
    finished: bool = False,
    stalled_notified: bool | None = None,
    touch_activity: bool = True,
) -> dict | None:
    state = load_task_state(user_id)
    if not state:
        return None

    state["updated_at"] = now_ts()
    if status is not None:
        state["status"] = status
    if last_progress is not None:
        state["last_progress"] = last_progress
        if touch_activity:
            state["last_activity_at"] = state["updated_at"]
    if error is not None:
        state["error"] = error
    if pid is not None:
        state["pid"] = pid
    if session_id is not None:
        state["session_id"] = session_id
    if finished:
        state["finished_at"] = state["updated_at"]
    if stalled_notified is not None:
        state["stalled_notified"] = stalled_notified

    await asyncio.to_thread(save_task_state, state)
    if finished:
        append_history(state)
    if last_progress:
        append_task_log(str(state["task_id"]), last_progress)
    return state


def create_task_state(user_id: int, prompt: str, prompt_preview: str, task_id: str):
    state = {
        "task_id": task_id,
        "user_id": user_id,
        "prompt": prompt,
        "prompt_preview": prompt_preview,
        "status": "queued",
        "created_at": now_ts(),
        "started_at": None,
        "updated_at": now_ts(),
        "finished_at": None,
        "last_progress": "任务已接收，等待执行",
        "last_activity_at": now_ts(),
        "error": None,
        "pid": None,
        "session_id": user_sessions.get(user_id),
        "stalled_notified": False,
    }
    save_task_state(state)
    append_task_log(task_id, "任务已接收，等待执行")
    return state


def compute_health(state: dict | None) -> str:
    if not state:
        return "unknown"
    if state.get("status") != "running":
        return state.get("status", "unknown")

    last_activity = float(state.get("last_activity_at") or state.get("updated_at") or now_ts())
    idle_for = now_ts() - last_activity
    if idle_for >= TASK_STALLED_AFTER:
        return "stalled"
    if idle_for >= TASK_QUIET_AFTER:
        return "quiet"
    return "active"


def format_status_text(state: dict | None) -> str:
    if not state:
        return "当前没有任务记录。"

    status = state.get("status", "unknown")
    health = compute_health(state)
    start_ts = state.get("started_at") or state.get("created_at") or now_ts()
    updated_ts = state.get("updated_at") or now_ts()
    lines = [
        f"任务状态：{status}",
        f"已运行：{human_duration(now_ts() - float(start_ts))}",
        f"最近进展：{state.get('last_progress') or '暂无'}",
        f"最近更新：{human_duration(now_ts() - float(updated_ts))}前",
    ]
    if status == "running":
        health_text = {
            "active": "active",
            "quiet": "quiet",
            "stalled": "stalled（疑似卡住）",
        }.get(health, health)
        lines.append(f"健康状态：{health_text}")
    if state.get("error"):
        lines.append(f"错误信息：{state['error'][:500]}")
    return "\n".join(lines)


def read_recent_logs(user_id: int, limit: int = 8) -> list[str]:
    state = load_task_state(user_id)
    if not state:
        return []

    log_path = task_log_file(str(state["task_id"]))
    if not log_path.exists():
        return []

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = [line.rstrip() for line in f.readlines() if line.strip()]
        return lines[-limit:]
    except Exception:
        logger.exception("read_recent_logs error user=%d", user_id)
        return []


# 每个用户的 claude session_id，用于多轮对话
user_sessions: dict[int, str] = load_sessions()
user_settings: dict[int, dict] = load_user_settings()


def build_claude_cmd(user_id: int, message: str) -> list[str]:
    session_id = user_sessions.get(user_id)
    model = get_user_model(user_id)
    cmd = [
        CLAUDE_CMD, "--dangerously-skip-permissions", "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", model,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(message)
    return cmd


def _summarize_tool_use(name: str, input_data: dict) -> str:
    """从工具调用中提取简洁的进度摘要。"""
    if name == "Edit":
        return f"编辑文件: {input_data.get('file_path', '?')}"
    if name == "Write":
        return f"写入文件: {input_data.get('file_path', '?')}"
    if name == "Read":
        return f"读取文件: {input_data.get('file_path', '?')}"
    if name == "Bash":
        cmd = input_data.get("command", "")
        return f"执行命令: {cmd[:80]}"
    if name == "Glob":
        return f"搜索文件: {input_data.get('pattern', '?')}"
    if name == "Grep":
        return f"搜索内容: {input_data.get('pattern', '?')}"
    if name in ("WebSearch", "WebFetch"):
        return f"网络搜索"
    if name == "TodoWrite":
        return "更新任务列表"
    if name == "Agent":
        return f"启动子代理"
    return f"执行: {name}"


async def ask_claude(
    user_id: int,
    message: str,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    _retry: int = 0,
) -> tuple[str, str | None]:
    cmd = build_claude_cmd(user_id, message)
    process = None
    collected_lines = []

    try:
        logger.info("starting claude task user=%d timeout=%ds", user_id, CLAUDE_TIMEOUT)
        workdir = get_user_workdir(user_id)

        # PTY で Node.js に TTY 環境を提供し、行バッファリングを強制
        master_fd, slave_fd = pty.openpty()
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=slave_fd,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
        os.close(slave_fd)  # 子进程已继承，父进程关闭 slave 端

        # スレッドで master fd から行単位で読み取るキュー
        line_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _pty_reader():
            """在后台线程中从 PTY master fd 逐行读取。"""
            buf = b""
            try:
                while True:
                    try:
                        chunk = os.read(master_fd, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        asyncio.run_coroutine_threadsafe(
                            line_queue.put(line), loop
                        )
            finally:
                asyncio.run_coroutine_threadsafe(line_queue.put(None), loop)
                with contextlib.suppress(OSError):
                    os.close(master_fd)

        loop = asyncio.get_event_loop()
        reader_thread = asyncio.get_event_loop().run_in_executor(None, _pty_reader)

        running_task = active_tasks.get(user_id)
        if running_task:
            running_task.process = process
            await update_task_state(
                user_id,
                status="running",
                pid=process.pid,
                last_progress="Claude 任务已启动",
            )

        result_text = ""
        session_id = None
        last_status_update = 0.0
        STATUS_UPDATE_INTERVAL = 3.0  # Telegram 状态消息最小更新间隔

        async def _maybe_update_status(progress: str):
            """限频更新 Telegram 状态消息。"""
            nonlocal last_status_update
            now = now_ts()
            if context and running_task and now - last_status_update >= STATUS_UPDATE_INTERVAL:
                last_status_update = now
                await update_status_message(
                    context,
                    running_task.status_message_chat_id,
                    running_task.status_message_id,
                    f"任务执行中...\n进度: {progress}\n摘要: {running_task.prompt_preview}",
                )

        async def _read_stream():
            nonlocal result_text, session_id, last_status_update
            seen_thinking = False
            seen_text = False
            turn_count = 0

            while True:
                raw_line = await line_queue.get()
                if raw_line is None:
                    break
                line = raw_line.decode("utf-8", errors="replace").replace("\r", "").strip()
                if not line:
                    continue
                collected_lines.append(line)

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                event_subtype = event.get("subtype", "")
                # debug: 记录每个事件类型
                if event_type == "assistant":
                    msg = event.get("message", {})
                    blocks = [b.get("type") for b in msg.get("content", [])]
                    logger.info("stream event: type=%s blocks=%s", event_type, blocks)
                elif event_type != "system":
                    logger.info("stream event: type=%s subtype=%s", event_type, event_subtype)

                # 提取 session_id（多个事件都可能包含）
                if event.get("session_id") and not session_id:
                    session_id = event["session_id"]

                # 处理 assistant 消息
                if event_type == "assistant":
                    msg = event.get("message", {})
                    content_blocks = msg.get("content", [])

                    # 检测新的 turn（stop_reason 非空说明该 turn 结束）
                    if msg.get("stop_reason"):
                        turn_count += 1
                        seen_thinking = False
                        seen_text = False

                    for block in content_blocks:
                        block_type = block.get("type")

                        if block_type == "thinking":
                            thinking_text = block.get("thinking", "")
                            if thinking_text:
                                # 取思考内容的前 80 字符作为摘要
                                preview = thinking_text.strip().replace("\n", " ")[:80]
                                progress = f"思考中: {preview}..."
                            else:
                                progress = "Claude 正在思考..."
                            seen_thinking = True
                            await update_task_state(user_id, last_progress=progress)
                            await _maybe_update_status(progress)

                        elif block_type == "tool_use":
                            tool_name = block.get("name", "?")
                            tool_input = block.get("input", {})
                            progress = _summarize_tool_use(tool_name, tool_input)
                            await update_task_state(user_id, last_progress=progress)
                            await _maybe_update_status(progress)

                        elif block_type == "text" and block.get("text") and not seen_text:
                            seen_text = True
                            progress = "Claude 正在回复..."
                            await update_task_state(user_id, last_progress=progress)
                            await _maybe_update_status(progress)

                # 最终结果事件
                elif event_type == "result":
                    result_text = event.get("result", "")
                    session_id = event.get("session_id") or session_id
                    # 检查是否是错误结果
                    if event.get("is_error"):
                        error_msg = result_text or "Claude 返回错误"
                        # 检查是否是 session 错误
                        if ("Invalid" in error_msg or "No conversation found" in error_msg) and user_sessions.get(user_id):
                            raise _SessionError(error_msg)
                        await update_task_state(
                            user_id,
                            status="error",
                            last_progress="任务执行失败",
                            error=error_msg[:1000],
                            finished=True,
                        )

        try:
            await asyncio.wait_for(_read_stream(), timeout=CLAUDE_TIMEOUT)
        except asyncio.TimeoutError:
            if process and process.returncode is None:
                process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            logger.warning("claude task timeout user=%d timeout=%ds", user_id, CLAUDE_TIMEOUT)
            await update_task_state(
                user_id,
                status="timeout",
                last_progress="任务超时",
                error=f"Claude 在 {CLAUDE_TIMEOUT} 秒内未完成",
                finished=True,
            )
            return (
                f"请求超时：Claude 在 {CLAUDE_TIMEOUT} 秒内未完成。"
                " 可以用 /status 查看任务状态，或用 /cancel 取消后重试。",
                None,
            )

        read_transport.close()
        # 等待进程退出
        await process.wait()

        if session_id:
            user_sessions[user_id] = session_id
            await asyncio.to_thread(save_sessions, user_sessions)
            await update_task_state(user_id, session_id=session_id)

        if not result_text and process.returncode != 0:
            stderr = ""
            if process.stderr:
                stderr_bytes = await process.stderr.read()
                stderr = stderr_bytes.decode("utf-8", errors="replace")
            logger.error("claude stderr: %s", stderr)
            await update_task_state(
                user_id,
                status="error",
                last_progress="任务执行失败",
                error=stderr[:1000] or f"Claude 返回非零退出码: {process.returncode}",
                finished=True,
            )
            return f"Claude 执行出错：{stderr[:500]}", None

        logger.info("claude task finished user=%d returncode=%s", user_id, process.returncode)
        return result_text or "（无回复）", session_id

    except _SessionError as e:
        logger.warning("session error detected, clearing session and retrying: %s", str(e)[:200])
        user_sessions.pop(user_id, None)
        await asyncio.to_thread(save_sessions, user_sessions)
        if _retry >= 1:
            logger.error("session retry exceeded, giving up")
            await update_task_state(
                user_id,
                status="error",
                last_progress="会话恢复失败",
                error="会话恢复失败，请用 /reset 重置后重试。",
                finished=True,
            )
            return "会话恢复失败，请用 /reset 重置后重试。", None
        return await ask_claude(user_id, message, context, _retry + 1)
    except asyncio.CancelledError:
        if process and process.returncode is None:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        raise
    except Exception as e:
        logger.exception("ask_claude error")
        await update_task_state(
            user_id,
            status="error",
            last_progress="任务执行失败",
            error=str(e),
            finished=True,
        )
        return f"发生错误：{e}", None


class _SessionError(Exception):
    """内部异常：session 无效需要重试。"""
    pass


async def send_message_chunked(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    max_length: int = 4000,
):
    # 超长输出以文件形式发送
    if len(text) > max_length * 3:
        import io
        doc = io.BytesIO(text.encode("utf-8"))
        doc.name = "output.txt"
        await context.bot.send_document(chat_id=chat_id, document=doc)
        return

    # 优先在换行符处切分
    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(0.3)
        await context.bot.send_message(chat_id=chat_id, text=chunk)


async def update_status_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    text: str,
):
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )
    except Exception:
        logger.debug("status message update skipped", exc_info=True)


async def stalled_monitor(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    while True:
        await asyncio.sleep(STALL_CHECK_INTERVAL)
        running = active_tasks.get(user_id)
        if not running or running.task.done():
            return

        state = load_task_state(user_id)
        if not state or state.get("status") != "running":
            continue

        if compute_health(state) != "stalled" or running.stalled_notified:
            continue

        running.stalled_notified = True
        await update_task_state(
            user_id,
            last_progress="最近没有新的执行反馈，任务疑似卡住",
            stalled_notified=True,
            touch_activity=False,
        )
        await update_status_message(
            context,
            running.status_message_chat_id,
            running.status_message_id,
            "任务执行中，但最近长时间没有新的执行反馈。\n"
            "状态：疑似卡住\n"
            f"摘要: {running.prompt_preview}",
        )
        await context.bot.send_message(
            chat_id=running.status_message_chat_id,
            text="任务疑似卡住。可用 /status 查看详情，或用 /cancel 取消。",
        )


async def run_claude_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    user_text: str,
):
    running = active_tasks[user_id]
    monitor_task = asyncio.create_task(stalled_monitor(context, user_id))

    try:
        if task_semaphore.locked():
            # 计算排队位置（当前运行中的任务数）
            queued_count = sum(
                1 for uid, rt in active_tasks.items()
                if uid != user_id and not rt.task.done()
            )
            queue_info = f"你前面有 {queued_count} 个任务" if queued_count > 0 else "即将开始"
            await update_task_state(user_id, status="queued", last_progress=f"任务排队中（{queue_info}）")
            await update_status_message(
                context,
                running.status_message_chat_id,
                running.status_message_id,
                f"任务排队中...（{queue_info}）\n"
                f"摘要: {running.prompt_preview}",
            )

        async with task_semaphore:
            state = await update_task_state(
                user_id,
                status="running",
                last_progress="任务开始执行",
            )
            if state and not state.get("started_at"):
                state["started_at"] = now_ts()
                await asyncio.to_thread(save_task_state, state)
            await update_status_message(
                context,
                running.status_message_chat_id,
                running.status_message_id,
                "任务执行中...\n"
                f"摘要: {running.prompt_preview}",
            )
            reply, _session_id = await ask_claude(user_id, user_text, context)

        elapsed = int(time.time() - running.started_at)
        final_state = load_task_state(user_id)
        if final_state and final_state.get("status") in {"timeout", "error"}:
            status_text = format_status_text(final_state)
            await update_status_message(
                context,
                running.status_message_chat_id,
                running.status_message_id,
                status_text,
            )
            await send_message_chunked(context, update.effective_chat.id, reply)
            logger.info("task ended user=%d status=%s", user_id, final_state.get("status"))
            return

        await update_task_state(
            user_id,
            status="done",
            last_progress="任务完成",
            finished=True,
        )
        summary = (
            "任务完成。\n"
            f"耗时: {elapsed} 秒\n"
            f"摘要: {running.prompt_preview}"
        )
        await update_status_message(
            context,
            running.status_message_chat_id,
            running.status_message_id,
            summary,
        )
        await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
        logger.info("task delivered user=%d elapsed=%ds", user_id, elapsed)
    except asyncio.CancelledError:
        await update_task_state(
            user_id,
            status="cancelled",
            last_progress="任务已取消",
            finished=True,
        )
        await update_status_message(
            context,
            running.status_message_chat_id,
            running.status_message_id,
            "任务已取消。\n"
            f"摘要: {running.prompt_preview}",
        )
        raise
    except Exception:
        await update_task_state(
            user_id,
            status="error",
            last_progress="任务执行失败",
            error="后台任务异常退出",
            finished=True,
        )
        await update_status_message(
            context,
            running.status_message_chat_id,
            running.status_message_id,
            "任务执行失败。\n"
            f"摘要: {running.prompt_preview}",
        )
        raise
    finally:
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
        # 只清理属于当前任务的条目，防止误删新任务
        if active_tasks.get(user_id) is running:
            active_tasks.pop(user_id, None)


def format_task_status(user_id: int) -> str:
    return format_status_text(load_task_state(user_id))


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("无权限使用此 Bot。")
        return
    await update.message.reply_text(
        "你好！我是 Claude Bot，直接发消息给我就能开始任务。\n"
        "输入 /help 查看所有可用命令。"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    await update.message.reply_text(
        "可用命令：\n\n"
        "/start — 显示欢迎消息\n"
        "/help — 显示此帮助信息\n"
        "/status — 查看当前任务状态\n"
        "/logs [n] — 查看最近 n 条任务日志（默认 8）\n"
        "/cancel — 取消当前任务\n"
        "/reset — 重置 Claude 会话\n"
        "/model [alias] — 查看或切换模型（sonnet/opus/haiku）\n"
        "/models — 显示可用模型列表\n"
        "/workdir [path] — 查看或切换工作目录\n"
        "/history [n] — 查看最近 n 个任务历史（默认 5）\n\n"
        "直接发送文本消息即可创建新任务。"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    if user_id in active_tasks:
        await update.message.reply_text("当前有运行中的任务，先用 /cancel 取消后再重置。")
        return
    user_sessions.pop(user_id, None)
    await asyncio.to_thread(save_sessions, user_sessions)
    await update.message.reply_text("对话已重置，开始新会话。")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("无权限使用此 Bot。")
        return

    user_text = update.message.text
    logger.info("user=%d msg_preview=%s", user_id, user_text[:80])
    logger.debug("user=%d full_prompt=%s", user_id, user_text)

    if user_id in active_tasks:
        await update.message.reply_text(
            "你已有任务在运行中。用 /status 查看状态，或用 /cancel 取消当前任务。"
        )
        return

    thinking_msg = await update.message.reply_text("任务已接收，准备启动...")
    prompt_preview = user_text.strip().replace("\n", " ")[:120] or "（空消息）"
    task_id = uuid.uuid4().hex[:12]
    create_task_state(user_id, user_text, prompt_preview, task_id)
    task = asyncio.create_task(run_claude_task(update, context, user_id, user_text))
    active_tasks[user_id] = RunningTask(
        task=task,
        process=None,
        started_at=time.time(),
        prompt_preview=prompt_preview,
        status_message_chat_id=thinking_msg.chat_id,
        status_message_id=thinking_msg.message_id,
        task_id=task_id,
    )

    def on_task_done(done_task: asyncio.Task):
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.info("task cancelled user=%d", user_id)
        except Exception:
            logger.exception("background task crashed user=%d", user_id)

    task.add_done_callback(on_task_done)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    await update.message.reply_text(format_task_status(user_id))


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    limit = 8
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 50))
        except ValueError:
            pass

    lines = read_recent_logs(user_id, limit=limit)
    if not lines:
        await update.message.reply_text("当前没有可查看的任务日志。")
        return

    await send_message_chunked(context, update.effective_chat.id, "\n".join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    running = active_tasks.get(user_id)
    if not running:
        await update.message.reply_text("当前没有可取消的任务。")
        return

    if running.process and running.process.returncode is None:
        running.process.kill()
        with contextlib.suppress(Exception):
            await running.process.wait()

    running.task.cancel()
    # 不在这里 pop active_tasks，由 run_claude_task 的 finally 统一清理
    logger.info("task cancelled by user user=%d", user_id)
    await update.message.reply_text("已取消当前任务。")


async def model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    current = get_user_model(user_id)
    current_name = next((name for mid, name in AVAILABLE_MODELS if mid == current), current)

    if not context.args:
        await update.message.reply_text(f"当前模型：{current_name}\n用 /models 查看可用模型列表。")
        return

    alias = context.args[0].lower()
    model_id = MODEL_ALIASES.get(alias, alias)
    model_name = next((name for mid, name in AVAILABLE_MODELS if mid == model_id), None)
    if not model_name:
        aliases = ", ".join(MODEL_ALIASES.keys())
        await update.message.reply_text(f"未知模型：{alias}\n可用简写：{aliases}")
        return

    await set_user_setting(user_id, "model", model_id)
    await update.message.reply_text(f"已切换到 {model_name}。")


async def models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    current = get_user_model(user_id)
    keyboard = [
        [InlineKeyboardButton(
            f"{'✅ ' if mid == current else ''}{name}",
            callback_data=f"model:{mid}"
        )]
        for mid, name in AVAILABLE_MODELS
    ]
    await update.message.reply_text(
        "选择模型：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def models_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_allowed(user_id):
        await query.answer("无权限。")
        return

    model_id = query.data.split(":", 1)[1]
    model_name = next((name for mid, name in AVAILABLE_MODELS if mid == model_id), None)
    if not model_name:
        await query.answer("未知模型。")
        return

    await set_user_setting(user_id, "model", model_id)
    keyboard = [
        [InlineKeyboardButton(
            f"{'✅ ' if mid == model_id else ''}{name}",
            callback_data=f"model:{mid}"
        )]
        for mid, name in AVAILABLE_MODELS
    ]
    await query.edit_message_text(
        f"已切换到 {model_name}。",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    await query.answer()


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    limit = 5
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 20))
        except ValueError:
            pass

    entries = load_history(user_id, limit=limit)
    if not entries:
        await update.message.reply_text("没有任务历史记录。")
        return

    lines = []
    for i, e in enumerate(reversed(entries), 1):
        created = ""
        if e.get("created_at"):
            created = time.strftime("%m-%d %H:%M", time.localtime(e["created_at"]))
        duration = ""
        if e.get("created_at") and e.get("finished_at"):
            dur = int(e["finished_at"] - e["created_at"])
            duration = f" ({human_duration(dur)})"
        status = e.get("status", "?")
        preview = e.get("prompt_preview", "")[:60]
        lines.append(f"{i}. [{status}] {created}{duration}\n   {preview}")

    await send_message_chunked(context, update.effective_chat.id, "\n".join(lines))


async def workdir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    if not context.args:
        current = get_user_workdir(user_id) or "（默认，bot 所在目录）"
        await update.message.reply_text(f"当前工作目录：{current}")
        return

    path = " ".join(context.args)
    resolved = os.path.realpath(path)
    if not resolved.startswith(ALLOWED_WORKDIR_PREFIX):
        await update.message.reply_text(
            f"只允许在 {ALLOWED_WORKDIR_PREFIX} 下的目录中工作。"
        )
        return
    if not os.path.isdir(resolved):
        await update.message.reply_text(f"路径不存在或不是目录：{path}")
        return

    await set_user_setting(user_id, "workdir", resolved)
    await update.message.reply_text(f"工作目录已切换到：{resolved}")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("请在 .env 中设置 TELEGRAM_BOT_TOKEN")
    if not ALLOWED_USER_IDS:
        raise SystemExit("ALLOWED_USER_IDS 未配置，拒绝启动（安全风险：任何人均可操控此 bot）")

    cleanup_old_logs()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("model", model))
    app.add_handler(CommandHandler("models", models))
    app.add_handler(CommandHandler("workdir", workdir))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CallbackQueryHandler(models_callback, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot 启动，开始 polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
