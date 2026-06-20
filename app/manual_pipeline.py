from __future__ import annotations

from app.openai_service import generate_text
from app.storage import db


LANGUAGE_NAMES = {
    "ko": "Korean",
    "en": "English",
    "es": "Spanish",
    "ar": "Arabic",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
}


def upsert_product_family(
    slug: str,
    display_name: str,
    manufacturer: str | None = None,
    model_group: str | None = None,
) -> int:
    with db() as conn:
        conn.execute(
            """
            insert into product_families(slug, display_name, manufacturer, model_group)
            values (?, ?, ?, ?)
            on conflict(slug) do update set
              display_name = excluded.display_name,
              manufacturer = excluded.manufacturer,
              model_group = excluded.model_group
            """,
            (slug, display_name, manufacturer, model_group),
        )
        row = conn.execute(
            "select id from product_families where slug = ?",
            (slug,),
        ).fetchone()
    return int(row["id"])


def create_manual_version_from_document(
    document_id: int,
    product_slug: str,
    display_name: str,
    language: str = "ko",
    manufacturer: str | None = None,
    model_group: str | None = None,
) -> int:
    product_family_id = upsert_product_family(
        product_slug,
        display_name,
        manufacturer=manufacturer,
        model_group=model_group,
    )
    with db() as conn:
        document = conn.execute(
            "select filename, page_count from documents where id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise ValueError(f"Document {document_id} was not found.")
        conn.execute(
            """
            insert into manual_versions(product_family_id, source_document_id, language, title)
            values (?, ?, ?, ?)
            on conflict(product_family_id, language) do update set
              source_document_id = excluded.source_document_id,
              title = excluded.title
            """,
            (product_family_id, document_id, language, display_name),
        )
        version = conn.execute(
            """
            select id from manual_versions
            where product_family_id = ? and language = ?
            """,
            (product_family_id, language),
        ).fetchone()
        version_id = int(version["id"])
        for page_number in range(1, int(document["page_count"]) + 1):
            rows = conn.execute(
                """
                select content from chunks
                where document_id = ? and page_number = ?
                order by id
                """,
                (document_id, page_number),
            ).fetchall()
            raw_text = "\n\n".join(row["content"] for row in rows)
            conn.execute(
                """
                insert into manual_pages(manual_version_id, page_number, raw_ocr_text, published_text, status)
                values (?, ?, ?, ?, ?)
                on conflict(manual_version_id, page_number) do update set
                  raw_ocr_text = excluded.raw_ocr_text,
                  published_text = coalesce(manual_pages.published_text, excluded.published_text),
                  updated_at = current_timestamp
                """,
                (version_id, page_number, raw_text, raw_text, "ocr_done" if raw_text else "raw"),
            )
    return version_id


def get_manual_page(manual_version_id: int, page_number: int) -> dict[str, object]:
    with db() as conn:
        row = conn.execute(
            """
            select mp.*, mv.language as source_language, mv.title, pf.slug, pf.display_name
            from manual_pages mp
            join manual_versions mv on mv.id = mp.manual_version_id
            join product_families pf on pf.id = mv.product_family_id
            where mp.manual_version_id = ? and mp.page_number = ?
            """,
            (manual_version_id, page_number),
        ).fetchone()
    if row is None:
        raise ValueError("Manual page was not found.")
    return dict(row)


def list_product_manuals() -> list[dict[str, object]]:
    with db() as conn:
        rows = conn.execute(
            """
            select
              pf.id as product_family_id,
              pf.slug,
              pf.display_name,
              pf.manufacturer,
              pf.model_group,
              pf.status as product_status,
              mv.id as manual_version_id,
              mv.language,
              mv.title,
              mv.status as manual_status,
              mv.source_document_id,
              d.filename as source_filename,
              coalesce(count(mp.id), 0) as page_count
            from product_families pf
            join manual_versions mv on mv.product_family_id = pf.id
            left join documents d on d.id = mv.source_document_id
            left join manual_pages mp on mp.manual_version_id = mv.id
            group by pf.id, mv.id
            order by pf.display_name, mv.language
            """
        ).fetchall()
    return [dict(row) for row in rows]


def ensure_translation_row(manual_page_id: int, language: str) -> int:
    with db() as conn:
        conn.execute(
            """
            insert into manual_page_translations(manual_page_id, language)
            values (?, ?)
            on conflict(manual_page_id, language) do nothing
            """,
            (manual_page_id, language),
        )
        row = conn.execute(
            """
            select id from manual_page_translations
            where manual_page_id = ? and language = ?
            """,
            (manual_page_id, language),
        ).fetchone()
    return int(row["id"])


def generate_translation_draft(manual_version_id: int, page_number: int, target_language: str) -> dict[str, object]:
    page = get_manual_page(manual_version_id, page_number)
    source_text = page.get("published_text") or page.get("ai_corrected_text") or page.get("raw_ocr_text") or ""
    language_name = LANGUAGE_NAMES.get(target_language, target_language)
    instructions = (
        "You are a professional industrial manual translator. Preserve numbers, model names, "
        "button labels, warnings, and step order. Do not invent missing content. If uncertain, mark [CHECK]."
    )
    prompt = (
        f"Translate this Korean manual page into {language_name}.\n\n"
        f"Product: {page['display_name']}\n"
        f"Page: {page_number}\n\n"
        f"Source text:\n{source_text}"
    )
    draft = generate_text(instructions, prompt)
    translation_id = ensure_translation_row(int(page["id"]), target_language)
    with db() as conn:
        conn.execute(
            """
            update manual_page_translations
            set draft_translation = ?, status = 'draft', updated_at = current_timestamp
            where id = ?
            """,
            (draft, translation_id),
        )
    return {"translation_id": translation_id, "status": "draft", "text": draft}


def check_translation_accuracy(manual_page_id: int, target_language: str) -> dict[str, object]:
    with db() as conn:
        row = conn.execute(
            """
            select mpt.*, mp.published_text, mp.raw_ocr_text, mp.page_number
            from manual_page_translations mpt
            join manual_pages mp on mp.id = mpt.manual_page_id
            where mpt.manual_page_id = ? and mpt.language = ?
            """,
            (manual_page_id, target_language),
        ).fetchone()
    if row is None or not row["draft_translation"]:
        raise ValueError("Draft translation does not exist.")

    source_text = row["published_text"] or row["raw_ocr_text"] or ""
    instructions = (
        "You are a technical accuracy editor. Compare source and translation. Fix mistranslations. "
        "Preserve all numbers, units, model names, warnings, and sequence. Add [CHECK: reason] for uncertain values."
    )
    prompt = (
        f"Source text:\n{source_text}\n\n"
        f"Draft translation:\n{row['draft_translation']}\n\n"
        "Return the corrected translation first, then a short 'Issues:' section."
    )
    checked = generate_text(instructions, prompt)
    with db() as conn:
        conn.execute(
            """
            update manual_page_translations
            set accuracy_checked_translation = ?, accuracy_issues = ?, status = 'accuracy_checked',
                updated_at = current_timestamp
            where id = ?
            """,
            (checked, checked, row["id"]),
        )
    return {"translation_id": row["id"], "status": "accuracy_checked", "text": checked}


def native_review_translation(manual_page_id: int, target_language: str) -> dict[str, object]:
    with db() as conn:
        row = conn.execute(
            """
            select * from manual_page_translations
            where manual_page_id = ? and language = ?
            """,
            (manual_page_id, target_language),
        ).fetchone()
    if row is None or not (row["accuracy_checked_translation"] or row["draft_translation"]):
        raise ValueError("Accuracy-checked translation does not exist.")

    base_text = row["accuracy_checked_translation"] or row["draft_translation"]
    language_name = LANGUAGE_NAMES.get(target_language, target_language)
    instructions = (
        f"You are a native {language_name} manual editor. Make the translation natural for customers. "
        "Do not change technical meaning, numbers, units, model names, warnings, or step order."
    )
    prompt = f"Review this manual translation for natural customer-facing {language_name}:\n\n{base_text}"
    final = generate_text(instructions, prompt)
    with db() as conn:
        conn.execute(
            """
            update manual_page_translations
            set final_translation = ?, native_review_notes = ?, status = 'native_reviewed',
                updated_at = current_timestamp
            where id = ?
            """,
            (final, final, row["id"]),
        )
    return {"translation_id": row["id"], "status": "native_reviewed", "text": final}
