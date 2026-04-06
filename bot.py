import os
import json
import subprocess
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = set(
    int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 每个用户的 claude session_id，用于多轮对话
user_sessions: dict[int, str] = {}


def ask_claude(user_id: int, message: str) -> str:
    session_id = user_sessions.get(user_id)

    cmd = ["claude", "-p", "--output-format", "json"]

    if session_id:
        cmd += ["--resume", session_id]

    cmd.append(message)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error("claude stderr: %s", result.stderr)
            return f"Claude 执行出错：{result.stderr[:200]}"

        data = json.loads(result.stdout)
        # 保存 session_id 供后续对话使用
        if "session_id" in data:
            user_sessions[user_id] = data["session_id"]

        return data.get("result", "（无回复）")

    except subprocess.TimeoutExpired:
        return "请求超时，请重试。"
    except json.JSONDecodeError:
        # fallback：直接返回原始输出
        return result.stdout.strip() or "解析响应失败。"
    except Exception as e:
        logger.exception("ask_claude error")
        return f"发生错误：{e}"


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
    user_sessions.pop(user_id, None)
    await update.message.reply_text("对话已重置，开始新会话。")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("无权限使用此 Bot。")
        return

    user_text = update.message.text
    logger.info("user=%d msg=%s", user_id, user_text[:80])

    # 发送"正在思考"提示
    thinking_msg = await update.message.reply_text("思考中...")

    reply = ask_claude(user_id, user_text)

    await thinking_msg.delete()
    await update.message.reply_text(reply)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("请在 .env 中设置 TELEGRAM_BOT_TOKEN")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot 启动，开始 polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
