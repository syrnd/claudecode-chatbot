import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = set(
    int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()
)

# claude CLI 路径和超时（默认10分钟）
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "/home/sikim/.npm-global/bin/claude")
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "600"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))
TASK_QUIET_AFTER = int(os.getenv("TASK_QUIET_AFTER", "180"))
TASK_STALLED_AFTER = int(os.getenv("TASK_STALLED_AFTER", "480"))
STALL_CHECK_INTERVAL = int(os.getenv("STALL_CHECK_INTERVAL", "60"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Session 持久化文件
SESSIONS_FILE = "/srv/claudecode-chatbot/sessions.json"
TASKS_DIR = Path("/srv/claudecode-chatbot/tasks")
sessions_lock = Lock()
task_state_lock = Lock()
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


def current_task_file(user_id: int) -> Path:
    return TASKS_DIR / f"user-{user_id}-current.json"


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


def update_task_state(
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

    save_task_state(state)
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


def build_claude_cmd(user_id: int, message: str) -> list[str]:
    session_id = user_sessions.get(user_id)
    cmd = [CLAUDE_CMD, "-p", "--output-format", "json"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd.append(message)
    return cmd


async def ask_claude(user_id: int, message: str) -> tuple[str, str | None]:
    cmd = build_claude_cmd(user_id, message)
    process = None

    try:
        logger.info("starting claude task user=%d timeout=%ds", user_id, CLAUDE_TIMEOUT)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        running_task = active_tasks.get(user_id)
        if running_task:
            running_task.process = process
            update_task_state(
                user_id,
                status="running",
                pid=process.pid,
                last_progress="Claude 任务已启动",
            )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=CLAUDE_TIMEOUT,
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if process.returncode != 0:
            # Try to parse stdout for structured error (e.g. invalid session)
            session_error = False
            try:
                err_data = json.loads(stdout)
                err_msg = err_data.get("result", "")
                if "Invalid" in err_msg and user_sessions.get(user_id):
                    logger.warning("session error detected, clearing session and retrying: %s", err_msg[:200])
                    user_sessions.pop(user_id, None)
                    await asyncio.to_thread(save_sessions, user_sessions)
                    session_error = True
            except Exception:
                pass

            if session_error:
                # Retry without session
                return await ask_claude(user_id, message)

            logger.error("claude stderr: %s", stderr)
            update_task_state(
                user_id,
                status="error",
                last_progress="任务执行失败",
                error=stderr[:1000] or f"Claude 返回非零退出码: {process.returncode}",
                finished=True,
            )
            return f"Claude 执行出错：{stderr[:500]}", None

        data = json.loads(stdout)
        session_id = data.get("session_id")
        if session_id:
            user_sessions[user_id] = session_id
            await asyncio.to_thread(save_sessions, user_sessions)
            update_task_state(user_id, session_id=session_id)

        logger.info("claude task finished user=%d returncode=%s", user_id, process.returncode)
        return data.get("result", "（无回复）"), session_id

    except asyncio.TimeoutError:
        if process and process.returncode is None:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        logger.warning("claude task timeout user=%d timeout=%ds", user_id, CLAUDE_TIMEOUT)
        update_task_state(
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
    except json.JSONDecodeError:
        # fallback：直接返回原始输出
        return stdout.strip() or "解析响应失败。", None
    except Exception as e:
        logger.exception("ask_claude error")
        update_task_state(
            user_id,
            status="error",
            last_progress="任务执行失败",
            error=str(e),
            finished=True,
        )
        return f"发生错误：{e}", None


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
        update_task_state(
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
            update_task_state(user_id, status="queued", last_progress="任务排队中")
            await update_status_message(
                context,
                running.status_message_chat_id,
                running.status_message_id,
                "任务排队中...\n"
                f"摘要: {running.prompt_preview}",
            )

        async with task_semaphore:
            state = update_task_state(
                user_id,
                status="running",
                last_progress="任务开始执行",
            )
            if state and not state.get("started_at"):
                state["started_at"] = now_ts()
                save_task_state(state)
            await update_status_message(
                context,
                running.status_message_chat_id,
                running.status_message_id,
                "任务执行中...\n"
                f"摘要: {running.prompt_preview}",
            )
            reply, _session_id = await ask_claude(user_id, user_text)

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
            await context.bot.send_message(chat_id=update.effective_chat.id, text=reply)
            logger.info("task ended user=%d status=%s", user_id, final_state.get("status"))
            return

        update_task_state(
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
        update_task_state(
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
        update_task_state(
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
    await update.message.reply_text("你好！我是 Claude Bot，直接发消息给我吧。")


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
    logger.info("user=%d msg=%s", user_id, user_text[:80])

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
            active_tasks.pop(user_id, None)

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

    lines = read_recent_logs(user_id)
    if not lines:
        await update.message.reply_text("当前没有可查看的任务日志。")
        return

    await update.message.reply_text("\n".join(lines))


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
    active_tasks.pop(user_id, None)
    logger.info("task cancelled by user user=%d", user_id)
    await update.message.reply_text("已取消当前任务。")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("请在 .env 中设置 TELEGRAM_BOT_TOKEN")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot 启动，开始 polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
