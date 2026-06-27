from __future__ import annotations

import html
import io
import json
import logging
import re
import sqlite3

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


def page_media(document_id: int | None, page_number: int) -> list[dict[str, object]]:
    if not document_id:
        return []
    try:
        with db() as conn:
            rows = conn.execute(
                """
                select media_type, title, alt_text, files_json
                from manual_page_media
                where document_id = ? and page_number = ? and is_published = 1
                order by id
                """,
                (document_id, page_number),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    media = []
    for row in rows:
        try:
            files = json.loads(row["files_json"] or "[]")
        except json.JSONDecodeError:
            files = []
        if files:
            media.append({**dict(row), "files": files})
    return media


def inline_manual_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def manual_page_html_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(inline_manual_html(" ".join(paragraph)))
            paragraph.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        if re.fullmatch(r"!\[[^\]]*\]\(/manual-media/\d+/\d+\)", line):
            flush_paragraph()
            continue
        heading = re.match(r"^#{1,3}\s+(.+)$", line)
        if heading:
            flush_paragraph()
            blocks.append(f"<b>{inline_manual_html(heading.group(1))}</b>")
            continue
        callout = re.match(
            r"^>\s*\*{0,2}(Warning|Caution|Note)(?::\*{0,2}|\*{1,2}:)?\s*(.*)$",
            line,
            re.I,
        )
        if callout:
            flush_paragraph()
            blocks.append(
                f"<b>{html.escape(callout.group(1).upper())}</b>\n{inline_manual_html(callout.group(2))}"
            )
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        numbered = re.match(r"^(\d+)[.)]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            blocks.append(f"• {inline_manual_html(bullet.group(1))}")
            continue
        if numbered:
            flush_paragraph()
            blocks.append(f"{numbered.group(1)}. {inline_manual_html(numbered.group(2))}")
            continue
        paragraph.append(line)
    flush_paragraph()
    return blocks


def group_telegram_blocks(blocks: list[str], limit: int = 3600) -> list[str]:
    messages: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            if current:
                messages.append(current)
                current = ""
            plain = re.sub(r"</?(?:b|code)>", "", block)
            messages.extend(plain[index : index + limit] for index in range(0, len(plain), limit))
            continue
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


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
                str(item["display_name"]),
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
    has_media: bool = False,
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
    nav_row.insert(
        1 if nav_row else 0,
        InlineKeyboardButton(f"{page_number} / {page_count}", callback_data="noop"),
    )
    buttons.append(nav_row)
    buttons.append(
        [
            InlineKeyboardButton("Page Summary", callback_data=f"summary:{manual_version_id}:{page_number}:{language}"),
            InlineKeyboardButton("Simple Explanation", callback_data=f"easy:{manual_version_id}:{page_number}:{language}"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton("Safety & Warnings", callback_data=f"warnings:{manual_version_id}:{page_number}:{language}"),
            InlineKeyboardButton("Specs & Values", callback_data=f"specs:{manual_version_id}:{page_number}:{language}"),
        ]
    )
    if has_media:
        buttons.append(
            [InlineKeyboardButton("View Page Media", callback_data=f"media:{manual_version_id}:{page_number}:{language}")]
        )
    buttons.append(
        [
            InlineKeyboardButton("Change language", callback_data=f"product_for_manual:{manual_version_id}"),
            InlineKeyboardButton("Products", callback_data="products"),
        ]
    )
    return InlineKeyboardMarkup(buttons)


def is_authenticated(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("customer_authenticated"))


async def begin_customer_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    context.user_data["login_step"] = "customer_id"
    text = (
        "Customer Login\n\n"
        "Enter your customer ID."
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text)
    else:
        await update.message.reply_text(text)


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authenticated(context):
        await begin_customer_login(update, context)
        return
    manuals = list_product_manuals()
    if not manuals:
        text = "No published preview manuals yet. Please register a manual in Manual Admin first."
        if update.callback_query:
            await update.callback_query.message.reply_text(text)
        else:
            await update.message.reply_text(text)
        return

    text = "Choose a product. The first manual page will open immediately."
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=product_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=product_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authenticated(context):
        await begin_customer_login(update, context)
        return
    await show_products(update, context)


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("Logged out. Use /start to sign in again.")


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
    source_language = str(info["language"]).lower()
    source_document_id = info.get("source_document_id")
    text = manual_page_text(manual_version_id, page_number)
    if text and selected_language != source_language:
        try:
            text = translate_customer_text(text, selected_language)
        except OpenAIUnavailable:
            pass

    caption = (
        f"{info['display_name']} · {language_label(selected_language)}\n"
        f"Original page {page_number} / {page_count}"
    )

    context.user_data["manual_version_id"] = manual_version_id
    context.user_data["source_document_id"] = source_document_id
    context.user_data["language"] = selected_language
    context.user_data["page_number"] = page_number

    media = page_media(int(source_document_id) if source_document_id else None, page_number)
    keyboard = page_keyboard(
        manual_version_id,
        page_number,
        page_count,
        selected_language,
        has_media=bool(media),
    )
    if source_document_id:
        try:
            image = render_page_png(int(source_document_id), page_number, dpi=150)
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(image),
                caption=caption[:1024],
            )
        except (ValueError, FileNotFoundError) as exc:
            LOGGER.warning("Could not render Telegram preview page: %s", exc)

    header = (
        f"<b>{html.escape(str(info['display_name']))}</b>\n"
        f"Digital Manual · {html.escape(language_label(selected_language))} · Page {page_number} / {page_count}"
    )
    body_blocks = manual_page_html_blocks(text) if text else ["No published page content is available."]
    compact_blocks: list[str] = []
    used = 0
    for block in body_blocks:
        if used + len(block) > 1800:
            if not compact_blocks:
                compact_blocks.append(re.sub(r"</?(?:b|code)>", "", block)[:1750] + "…")
            break
        compact_blocks.append(block)
        used += len(block)
    compact_blocks.append("Type a question below, or choose one of the four review tools.")
    messages = group_telegram_blocks([header, *compact_blocks])
    for index, message in enumerate(messages):
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="HTML",
            reply_markup=keyboard if index == len(messages) - 1 else None,
            disable_web_page_preview=True,
        )


async def manual_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authenticated(context):
        await begin_customer_login(update, context)
        return
    if not context.args:
        await manuals_command(update, context)
        return
    try:
        manual_version_id = int(context.args[0])
        page_number = int(context.args[1]) if len(context.args) > 1 else 1
    except ValueError:
        await update.message.reply_text("Please use /manuals and choose with buttons.")
        return
    await send_manual_page(
        update.effective_chat.id,
        context,
        manual_version_id,
        page_number,
        str(context.user_data.get("language") or "en"),
    )


async def product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if not is_authenticated(context):
        await begin_customer_login(update, context)
        return

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
    preferred_language = str(context.user_data.get("language") or "en")
    manuals_by_language = {str(manual["language"]).lower(): manual for manual in manuals}
    selected = (
        manuals_by_language.get(preferred_language)
        or manuals_by_language.get("en")
        or manuals_by_language.get("ko")
        or manuals[0]
    )
    await send_manual_page(
        query.message.chat_id,
        context,
        int(selected["manual_version_id"]),
        1,
        preferred_language,
    )


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_authenticated(context):
        await begin_customer_login(update, context)
        return
    if (query.data or "") == "noop":
        return
    parts = (query.data or "").split(":")
    action, manual_id_raw, page_raw = parts[:3]
    language = parts[3] if len(parts) > 3 else str(context.user_data.get("language") or "en")
    manual_version_id = int(manual_id_raw)
    page_number = int(page_raw)

    if action in {"page", "manual", "manual_lang"}:
        await send_manual_page(query.message.chat_id, context, manual_version_id, page_number, language)
        return

    info = manual_version_info(manual_version_id)
    if info is None:
        await query.message.reply_text("Manual not found.")
        return
    if action == "media":
        media_items = page_media(
            int(info["source_document_id"]) if info.get("source_document_id") else None,
            page_number,
        )
        if not media_items:
            await query.message.reply_text("No additional media is registered for this page.")
            return
        for item in media_items:
            filename = str(item["files"][0])
            path = (settings.manual_media_dir / filename).resolve()
            try:
                path.relative_to(settings.manual_media_dir.resolve())
            except ValueError:
                continue
            if not path.exists():
                continue
            title = str(item["title"])
            if item["media_type"] == "gif" or path.suffix.lower() == ".gif":
                await context.bot.send_animation(query.message.chat_id, animation=path, caption=title[:1024])
            else:
                note = "\n360° preview: first frame" if item["media_type"] == "spin" else ""
                await context.bot.send_photo(query.message.chat_id, photo=path, caption=(title + note)[:1024])
        return
    result = page_action(
        action,
        manual_page_text(manual_version_id, page_number),
        info["display_name"],
        page_number,
        language,
    )
    answer_parts = [str(result.get("title") or "Page Review"), str(result.get("answer") or "")]
    answer_parts.extend(f"• {item}" for item in result.get("bullets", []))
    answer = "\n\n".join(part for part in answer_parts if part)
    source_language = str(info["language"]).lower()
    if result.get("engine") == "fallback" and source_language != language.lower():
        try:
            answer = translate_customer_text(answer, language)
        except OpenAIUnavailable:
            pass
    await query.message.reply_text(answer[:3900])


async def answer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (update.message.text or "").strip()
    login_step = context.user_data.get("login_step")
    if login_step == "customer_id":
        context.user_data["customer_id"] = message or "customer"
        context.user_data["login_step"] = "password"
        await update.message.reply_text("Enter your password.")
        return
    if login_step == "password":
        customer_id = str(context.user_data.get("customer_id") or "Customer")
        context.user_data.pop("login_step", None)
        context.user_data["customer_authenticated"] = True
        context.user_data["language"] = "en"
        manuals = list_product_manuals()
        text = f"Welcome, {customer_id}. Choose a product." if manuals else "Login complete. No Preview manuals are published yet."
        await update.message.reply_text(text, reply_markup=product_keyboard() if manuals else None)
        return
    if not is_authenticated(context):
        await begin_customer_login(update, context)
        return
    question = message
    source_document_id = context.user_data.get("source_document_id")
    if not source_document_id:
        await update.message.reply_text("Please use /manuals first, then choose product and language.")
        return
    language = str(context.user_data.get("language") or "en")
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
    application.add_handler(CommandHandler("logout", logout_command))
    application.add_handler(CommandHandler("manuals", manuals_command))
    application.add_handler(CommandHandler("preview", manuals_command))
    application.add_handler(CommandHandler("manual", manual_command))
    application.add_handler(CommandHandler("manula", manual_command))
    application.add_handler(CallbackQueryHandler(product_callback, pattern=r"^(products|product:|product_for_manual:|languages_more:)"))
    application.add_handler(CallbackQueryHandler(page_callback, pattern=r"^(noop$|(manual|manual_lang|page|summary|easy|warnings|specs|media):)"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, answer_message))
    LOGGER.info("Telegram customer manual viewer started")
    application.run_polling()


if __name__ == "__main__":
    main()
