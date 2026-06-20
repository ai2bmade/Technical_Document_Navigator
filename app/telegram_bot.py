from __future__ import annotations

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
from app.manual_pipeline import get_manual_page, list_product_manuals
from app.page_images import render_page_png
from app.storage import db, init_db


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)


def manual_version_info(manual_version_id: int) -> dict[str, object] | None:
    with db() as conn:
        row = conn.execute(
            """
            select
              mv.id as manual_version_id,
              mv.language,
              mv.title,
              mv.source_document_id,
              pf.slug,
              pf.display_name,
              coalesce(count(mp.id), 0) as page_count
            from manual_versions mv
            join product_families pf on pf.id = mv.product_family_id
            left join manual_pages mp on mp.manual_version_id = mv.id
            where mv.id = ?
            group by mv.id
            """,
            (manual_version_id,),
        ).fetchone()
    return dict(row) if row else None


def manual_page_text(manual_version_id: int, page_number: int) -> str:
    page = get_manual_page(manual_version_id, page_number)
    return (
        page.get("published_text")
        or page.get("ai_corrected_text")
        or page.get("raw_ocr_text")
        or ""
    )


def page_keyboard(manual_version_id: int, page_number: int, page_count: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page_number > 1:
        nav_row.append(InlineKeyboardButton("Prev", callback_data=f"page:{manual_version_id}:{page_number - 1}"))
    if page_number < page_count:
        nav_row.append(InlineKeyboardButton("Next", callback_data=f"page:{manual_version_id}:{page_number + 1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append(
        [
            InlineKeyboardButton("현재 페이지 요약", callback_data=f"summary:{manual_version_id}:{page_number}"),
            InlineKeyboardButton("주의사항", callback_data=f"warnings:{manual_version_id}:{page_number}"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Technical Document Navigator\n\n"
        "고객용 모바일 매뉴얼 뷰어입니다.\n\n"
        "/manuals - 볼 수 있는 매뉴얼 목록\n"
        "/manual <manual_id> [page] - 매뉴얼 페이지 보기\n"
        "/help - 도움말\n\n"
        "예: /manual 1 3\n"
        "질문을 보내면 마지막으로 연 매뉴얼을 기준으로 답합니다."
    )


async def manuals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manuals = list_product_manuals()
    if not manuals:
        await update.message.reply_text("아직 Preview에 공개된 매뉴얼이 없습니다.")
        return
    lines = ["Preview 매뉴얼:"]
    for manual in manuals:
        lines.append(
            f"- #{manual['manual_version_id']} {manual['display_name']} "
            f"({manual['language'].upper()}, {manual['page_count']} pages)\n"
            f"  보기: /manual {manual['manual_version_id']} 1"
        )
    await update.message.reply_text("\n".join(lines))


async def send_manual_page(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    manual_version_id: int,
    page_number: int,
) -> None:
    info = manual_version_info(manual_version_id)
    if info is None:
        await context.bot.send_message(chat_id, "매뉴얼을 찾을 수 없습니다.")
        return

    page_count = int(info["page_count"])
    page_number = max(1, min(page_number, page_count))
    source_document_id = info.get("source_document_id")
    text = manual_page_text(manual_version_id, page_number)
    caption = (
        f"{info['display_name']} ({info['language'].upper()})\n"
        f"Page {page_number} / {page_count}"
    )
    if text:
        caption += "\n\n" + text[:700]

    context.user_data["manual_version_id"] = manual_version_id
    context.user_data["source_document_id"] = source_document_id
    if source_document_id:
        image = render_page_png(int(source_document_id), page_number, dpi=135)
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=io.BytesIO(image),
            caption=caption[:1024],
            reply_markup=page_keyboard(manual_version_id, page_number, page_count),
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption[:3900],
            reply_markup=page_keyboard(manual_version_id, page_number, page_count),
        )


async def manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await manuals_command(update, context)
        return
    try:
        manual_version_id = int(context.args[0])
        page_number = int(context.args[1]) if len(context.args) > 1 else 1
    except ValueError:
        await update.message.reply_text("사용법: /manual <manual_id> [page]")
        return
    await send_manual_page(update.effective_chat.id, context, manual_version_id, page_number)


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, manual_id_raw, page_raw = query.data.split(":")
    manual_version_id = int(manual_id_raw)
    page_number = int(page_raw)

    if action == "page":
        await send_manual_page(query.message.chat_id, context, manual_version_id, page_number)
        return

    info = manual_version_info(manual_version_id)
    if info is None:
        await query.message.reply_text("매뉴얼을 찾을 수 없습니다.")
        return
    result = page_action(
        action,
        manual_page_text(manual_version_id, page_number),
        info["display_name"],
        page_number,
    )
    await query.message.reply_text(result["answer"][:3900])


async def answer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = update.message.text or ""
    source_document_id = context.user_data.get("source_document_id")
    if not source_document_id:
        await update.message.reply_text(
            "먼저 /manuals 로 매뉴얼을 고른 뒤 질문해 주세요."
        )
        return
    result = answer_question(question, int(source_document_id) if source_document_id else None)
    await update.message.reply_text(result["answer"][:3900])


def main() -> None:
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
    application.run_polling()


if __name__ == "__main__":
    main()
