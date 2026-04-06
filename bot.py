import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Session 持久化文件
SESSIONS_FILE = "/srv/claudecode-chatbot/sessions.json"
sessions_lock = Lock()
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)


@dataclass
class RunningTask:
    task: asyncio.Task
    process: asyncio.subprocess.Process | None
    started_at: float
    prompt_preview: str
    status_message_chat_id: int
    status_message_id: int


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

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=CLAUDE_TIMEOUT,
        )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if process.returncode != 0:
            logger.error("claude stderr: %s", stderr)
            return f"Claude 执行出错：{stderr[:500]}", None

        data = json.loads(stdout)
        session_id = data.get("session_id")
        if session_id:
            user_sessions[user_id] = session_id
            await asyncio.to_thread(save_sessions, user_sessions)

        logger.info("claude task finished user=%d returncode=%s", user_id, process.returncode)
        return data.get("result", "（无回复）"), session_id

    except asyncio.TimeoutError:
        if process and process.returncode is None:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
        logger.warning("claude task timeout user=%d timeout=%ds", user_id, CLAUDE_TIMEOUT)
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


async def run_claude_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    user_text: str,
):
    running = active_tasks[user_id]

    try:
        if task_semaphore.locked():
            await update_status_message(
                context,
                running.status_message_chat_id,
                running.status_message_id,
                "任务排队中...\n"
                f"摘要: {running.prompt_preview}",
            )

        async with task_semaphore:
            await update_status_message(
                context,
                running.status_message_chat_id,
                running.status_message_id,
                "任务执行中...\n"
                f"摘要: {running.prompt_preview}",
            )
            reply, _session_id = await ask_claude(user_id, user_text)

        elapsed = int(time.time() - running.started_at)
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
        await update_status_message(
            context,
            running.status_message_chat_id,
            running.status_message_id,
            "任务已取消。\n"
            f"摘要: {running.prompt_preview}",
        )
        raise
    except Exception:
        await update_status_message(
            context,
            running.status_message_chat_id,
            running.status_message_id,
            "任务执行失败。\n"
            f"摘要: {running.prompt_preview}",
        )
        raise
    finally:
        active_tasks.pop(user_id, None)


def format_task_status(user_id: int) -> str:
    running = active_tasks.get(user_id)
    if not running:
        return "当前没有运行中的任务。"

    elapsed = int(time.time() - running.started_at)
    return (
        "当前有一个任务正在运行。\n"
        f"已运行: {elapsed} 秒\n"
        f"摘要: {running.prompt_preview}"
    )


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
    task = asyncio.create_task(run_claude_task(update, context, user_id, user_text))
    active_tasks[user_id] = RunningTask(
        task=task,
        process=None,
        started_at=time.time(),
        prompt_preview=prompt_preview,
        status_message_chat_id=thinking_msg.chat_id,
        status_message_id=thinking_msg.message_id,
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
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot 启动，开始 polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
