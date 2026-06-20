from __future__ import annotations

import asyncio
import io
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.copilot import answer_question, page_action
from app.page_images import render_page_png
from app.storage import db, init_db


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)


def list_indexed_documents() -> list[dict[str, object]]:
    with db() as conn:
        rows = conn.execute(
            """
            select d.id, d.filename, d.page_count, count(c.id) as chunk_count
            from documents d
            left join chunks c on c.document_id = d.id
            group by d.id
            having count(c.id) > 0
            order by d.id desc
            """
        ).fetchall()
    return [dict(row) for row in rows]


def page_text(document_id: int, page_number: int) -> str:
    with db() as conn:
        rows = conn.execute(
            """
            select content from chunks
            where document_id = ? and page_number = ?
            order by id
            """,
            (document_id, page_number),
        ).fetchall()
    return "\n\n".join(row["content"] for row in rows)


def document_info(document_id: int) -> dict[str, object] | None:
    with db() as conn:
        row = conn.execute(
            "select id, filename, page_count from documents where id = ?",
            (document_id,),
        ).fetchone()
    return dict(row) if row else None


def page_keyboard(document_id: int, page_number: int, page_count: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page_number > 1:
        nav_row.append(InlineKeyboardButton("Prev", callback_data=f"page:{document_id}:{page_number - 1}"))
    if page_number < page_count:
        nav_row.append(InlineKeyboardButton("Next", callback_data=f"page:{document_id}:{page_number + 1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append(
        [
            InlineKeyboardButton("현재 페이지 요약", callback_data=f"summary:{document_id}:{page_number}"),
            InlineKeyboardButton("주의사항", callback_data=f"warnings:{document_id}:{page_number}"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Technical Document Navigator\n\n"
        "고객용 모바일 매뉴얼 뷰어입니다.\n\n"
        "/manuals - 볼 수 있는 매뉴얼 목록\n"
        "/manual <id> [page] - 매뉴얼 페이지 보기\n"
        "/help - 도움말\n\n"
        "예: /manual 1 3\n"
        "질문을 보내면 인덱싱된 매뉴얼에서 근거를 찾아 답합니다."
    )


async def manuals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    documents = list_indexed_documents()
    if not documents:
        await update.message.reply_text("아직 공개 가능한 인덱싱 매뉴얼이 없습니다.")
        return
    lines = ["볼 수 있는 매뉴얼:"]
    for doc in documents:
        lines.append(
            f"- #{doc['id']} {doc['filename']} ({doc['page_count']} pages)\n"
            f"  보기: /manual {doc['id']} 1"
        )
    await update.message.reply_text("\n".join(lines))


async def send_manual_page(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    document_id: int,
    page_number: int,
) -> None:
    info = document_info(document_id)
    if info is None:
        await context.bot.send_message(chat_id, "매뉴얼을 찾을 수 없습니다.")
        return

    page_count = int(info["page_count"])
    page_number = max(1, min(page_number, page_count))
    image = render_page_png(document_id, page_number, dpi=135)
    text = page_text(document_id, page_number)
    caption = f"{info['filename']}\nPage {page_number} / {page_count}"
    if text:
        caption += "\n\n" + text[:700]

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=io.BytesIO(image),
        caption=caption[:1024],
        reply_markup=page_keyboard(document_id, page_number, page_count),
    )


async def manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await manuals_command(update, context)
        return
    try:
        document_id = int(context.args[0])
        page_number = int(context.args[1]) if len(context.args) > 1 else 1
    except ValueError:
        await update.message.reply_text("사용법: /manual <id> [page]")
        return
    await send_manual_page(update.effective_chat.id, context, document_id, page_number)


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, doc_id_raw, page_raw = query.data.split(":")
    document_id = int(doc_id_raw)
    page_number = int(page_raw)

    if action == "page":
        await send_manual_page(query.message.chat_id, context, document_id, page_number)
        return

    info = document_info(document_id)
    if info is None:
        await query.message.reply_text("매뉴얼을 찾을 수 없습니다.")
        return
    result = page_action(action, page_text(document_id, page_number), info["filename"], page_number)
    await query.message.reply_text(result["answer"][:3900])


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
    application.add_handler(CommandHandler("manuals", manuals_command))
    application.add_handler(CommandHandler("manual", manual_command))
    application.add_handler(CallbackQueryHandler(page_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, answer_message))
    LOGGER.info("Telegram customer manual viewer started")
    await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
