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
        "/manual - manual mode\n/spec - specification mode\n/review - layout review mode\n/help - help"
    )


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = update.message.text or ""
    context.user_data["mode"] = command.strip("/")
    await update.message.reply_text(f"{context.user_data['mode']} mode selected. Send a question.")


async def answer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = update.message.text or ""
    result = answer_question(question)
    await update.message.reply_text(result["answer"])


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    init_db()
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("manual", mode_command))
    application.add_handler(CommandHandler("spec", mode_command))
    application.add_handler(CommandHandler("review", mode_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, answer_message))
    LOGGER.info("Telegram bot started")
    await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
