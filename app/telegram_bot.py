from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import settings
from app.copilot import answer_question
from app.storage import init_db


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Technical Document Navigator\n\n"
        "/manual - 매뉴얼 질문 모드\n"
        "/spec - 사양서 질문 모드\n"
        "/review - 레이아웃 검토 모드\n"
        "/help - 도움말\n\n"
        "지금 MVP에서는 웹에서 PDF 업로드/OCR을 먼저 하고, 텔레그램에서는 인덱싱된 문서에 질문합니다."
    )


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = update.message.text or ""
    context.user_data["mode"] = command.strip("/")
    labels = {
        "manual": "매뉴얼",
        "spec": "사양서",
        "review": "레이아웃 검토",
    }
    label = labels.get(context.user_data["mode"], context.user_data["mode"])
    await update.message.reply_text(f"{label} 모드입니다. 질문을 보내주세요.")


async def answer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = update.message.text or ""
    result = answer_question(question)
    await update.message.reply_text(result["answer"][:3900])


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    init_db()
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("manual", mode_command))
    application.add_handler(CommandHandler("spec", mode_command))
    application.add_handler(CommandHandler("review", mode_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, answer_message))
    LOGGER.info("Telegram bot started")
    await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
