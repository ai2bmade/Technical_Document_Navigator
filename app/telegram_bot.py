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
from app.copilot import answer_question_with_ai, page_action, translate_customer_text
from app.manual_pipeline import get_manual_page, list_product_manuals
from app.openai_service import OpenAIUnavailable
from app.page_images import render_page_png
from app.storage import db, init_db


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)


LANGUAGE_LABELS = {
    "ko": "한국어",
    "en": "English",
    "es": "Español",
    "ar": "Arabic",
    "fr": "Français",
    "de": "Deutsch",
    "pt": "Português",
}

CUSTOMER_LANGUAGES = ["ko", "en", "es", "ar", "fr", "de", "pt"]
PRIMARY_LANGUAGES = ["ko", "en", "es", "ar"]
SECONDARY_LANGUAGES = ["fr", "de", "pt"]


def language_label(code: str) -> str:
    return LANGUAGE_LABELS.get(code.lower(), code.upper())


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


def product_keyboard() -> InlineKeyboardMarkup:
    grouped: dict[str, dict[str, object]] = {}
    for manual in list_product_manuals():
        slug = str(manual["slug"])
        grouped.setdefault(
            slug,
            {
                "slug": slug,
                "display_name": manual["display_name"],
                "languages": [],
            },
        )
        grouped[slug]["languages"].append(str(manual["language"]).upper())

    rows = [
        [
            InlineKeyboardButton(
                f"{item['display_name']} ({', '.join(item['languages'])})",
                callback_data=f"product:{item['slug']}",
            )
        ]
        for item in grouped.values()
    ]
    return InlineKeyboardMarkup(rows)


def language_keyboard(product_slug: str, expanded: bool = False) -> InlineKeyboardMarkup:
    manuals = [
        manual
        for manual in list_product_manuals()
        if str(manual["slug"]) == product_slug
    ]
    if not manuals:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Choose product again", callback_data="products")]])

    manuals_by_language = {str(manual["language"]).lower(): manual for manual in manuals}
    fallback_manual = manuals_by_language.get("ko", manuals[0])
    languages = CUSTOMER_LANGUAGES if expanded else PRIMARY_LANGUAGES
    buttons = []
    for language in languages:
        manual = manuals_by_language.get(language, fallback_manual)
        buttons.append(
            InlineKeyboardButton(
                language_label(language),
                callback_data=f"manual_lang:{manual['manual_version_id']}:1:{language}",
            )
        )
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    if not expanded:
        rows.append([InlineKeyboardButton("More languages", callback_data=f"languages_more:{product_slug}")])
    rows.append([InlineKeyboardButton("Choose product again", callback_data="products")])
    return InlineKeyboardMarkup(rows)


def page_keyboard(
    manual_version_id: int,
    page_number: int,
    page_count: int,
    language: str,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page_number > 1:
        nav_row.append(
            InlineKeyboardButton(
                "Prev",
                callback_data=f"page:{manual_version_id}:{page_number - 1}:{language}",
            )
        )
    if page_number < page_count:
        nav_row.append(
            InlineKeyboardButton(
                "Next",
                callback_data=f"page:{manual_version_id}:{page_number + 1}:{language}",
            )
        )
    if nav_row:
        buttons.append(nav_row)
    buttons.append(
        [
            InlineKeyboardButton("Summary", callback_data=f"summary:{manual_version_id}:{page_number}:{language}"),
            InlineKeyboardButton("Warnings", callback_data=f"warnings:{manual_version_id}:{page_number}:{language}"),
        ]
    )
    buttons.append([InlineKeyboardButton("Change language", callback_data=f"product_for_manual:{manual_version_id}")])
    return InlineKeyboardMarkup(buttons)


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manuals = list_product_manuals()
    if not manuals:
        text = "No published preview manuals yet. Please register a manual in Manual Admin first."
        if update.callback_query:
            await update.callback_query.message.reply_text(text)
        else:
            await update.message.reply_text(text)
        return

    text = "Choose a product manual. After selecting a product, choose the customer language."
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=product_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=product_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Technical Document Navigator\n\n"
        "Choose a product and language with the buttons below. "
        "Then you can view pages or ask questions about the selected manual.",
        reply_markup=product_keyboard() if list_product_manuals() else None,
    )


async def manuals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_products(update, context)


async def send_manual_page(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    manual_version_id: int,
    page_number: int,
    language: str | None = None,
) -> None:
    info = manual_version_info(manual_version_id)
    if info is None:
        await context.bot.send_message(chat_id, "Manual not found.")
        return

    page_count = int(info["page_count"])
    page_number = max(1, min(page_number, page_count))
    selected_language = (language or str(info["language"])).lower()
    source_document_id = info.get("source_document_id")
    text = manual_page_text(manual_version_id, page_number)
    if text:
        try:
            text = translate_customer_text(text, selected_language)
        except OpenAIUnavailable:
            pass

    caption = (
        f"{info['display_name']}\n"
        f"Language: {language_label(selected_language)}\n"
        f"Page {page_number} / {page_count}"
    )
    if text:
        caption += "\n\n" + text[:700]

    context.user_data["manual_version_id"] = manual_version_id
    context.user_data["source_document_id"] = source_document_id
    context.user_data["language"] = selected_language
    context.user_data["page_number"] = page_number

    keyboard = page_keyboard(manual_version_id, page_number, page_count, selected_language)
    if source_document_id:
        image = render_page_png(int(source_document_id), page_number, dpi=135)
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=io.BytesIO(image),
            caption=caption[:1024],
            reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text=caption[:3900], reply_markup=keyboard)


async def manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await manuals_command(update, context)
        return
    try:
        manual_version_id = int(context.args[0])
        page_number = int(context.args[1]) if len(context.args) > 1 else 1
    except ValueError:
        await update.message.reply_text("Please use /manuals and choose with buttons.")
        return
    await send_manual_page(update.effective_chat.id, context, manual_version_id, page_number)


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "products":
        await show_products(update, context)
        return

    if data.startswith("languages_more:"):
        product_slug = data.split(":", 1)[1]
        manuals = [manual for manual in list_product_manuals() if str(manual["slug"]) == product_slug]
        display_name = manuals[0]["display_name"] if manuals else "this product"
        await query.message.reply_text(
            f"More languages for {display_name}.",
            reply_markup=language_keyboard(product_slug, expanded=True),
        )
        return

    if data.startswith("product_for_manual:"):
        manual_version_id = int(data.split(":", 1)[1])
        info = manual_version_info(manual_version_id)
        if info is None:
            await query.message.reply_text("Manual not found.")
            return
        await query.message.reply_text(
            f"Choose language for {info['display_name']}.",
            reply_markup=language_keyboard(str(info["slug"])),
        )
        return

    product_slug = data.split(":", 1)[1]
    manuals = [manual for manual in list_product_manuals() if str(manual["slug"]) == product_slug]
    if not manuals:
        await query.message.reply_text("No language version is registered for this product.")
        return
    await query.message.reply_text(
        f"Choose language for {manuals[0]['display_name']}.",
        reply_markup=language_keyboard(product_slug),
    )


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    action, manual_id_raw, page_raw = parts[:3]
    language = parts[3] if len(parts) > 3 else str(context.user_data.get("language") or "ko")
    manual_version_id = int(manual_id_raw)
    page_number = int(page_raw)

    if action in {"page", "manual", "manual_lang"}:
        await send_manual_page(query.message.chat_id, context, manual_version_id, page_number, language)
        return

    info = manual_version_info(manual_version_id)
    if info is None:
        await query.message.reply_text("Manual not found.")
        return
    result = page_action(
        action,
        manual_page_text(manual_version_id, page_number),
        info["display_name"],
        page_number,
    )
    answer = result["answer"]
    try:
        answer = translate_customer_text(answer, language)
    except OpenAIUnavailable:
        pass
    await query.message.reply_text(answer[:3900])


async def answer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = update.message.text or ""
    source_document_id = context.user_data.get("source_document_id")
    if not source_document_id:
        await update.message.reply_text("Please use /manuals first, then choose product and language.")
        return
    language = str(context.user_data.get("language") or "ko")
    try:
        result = answer_question_with_ai(question, int(source_document_id), language=language)
        await update.message.reply_text(result["answer"][:3900])
    except OpenAIUnavailable:
        await update.message.reply_text("AI answer engine is not available. Please try again later.")


def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")
    init_db()
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("manuals", manuals_command))
    application.add_handler(CommandHandler("manual", manual_command))
    application.add_handler(CommandHandler("manula", manual_command))
    application.add_handler(CallbackQueryHandler(product_callback, pattern=r"^(products|product:|product_for_manual:|languages_more:)"))
    application.add_handler(CallbackQueryHandler(page_callback, pattern=r"^(manual|manual_lang|page|summary|warnings):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, answer_message))
    LOGGER.info("Telegram customer manual viewer started")
    application.run_polling()


if __name__ == "__main__":
    main()
