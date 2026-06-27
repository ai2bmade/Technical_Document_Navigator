from __future__ import annotations

import json
import html
import re
import shutil
import sqlite3
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import settings
from app.copilot import (
    analyze_spec,
    answer_question_with_ai,
    answer_question,
    page_action,
    review_layout,
    translation_workflow,
)
from app.knowledge_pipeline import build_manual_knowledge, correct_document_pages
from app.manual_pipeline import (
    add_manual_page_block,
    check_translation_accuracy,
    create_manual_version_from_document,
    create_reviewed_page_translation,
    create_reviewed_translation_version,
    delete_manual_page_block,
    generate_translation_draft,
    list_manual_page_blocks,
    list_product_manuals,
    native_review_translation,
    sync_manual_versions_from_document,
    update_manual_page_block,
)
from app.openai_service import OpenAIUnavailable
from app.ocr import ocr_page_region, run_ocr_for_document
from app.page_images import render_page_png
from app.pdf_ingest import ingest_pdf, split_chunks
from app.storage import db, init_db


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
MEDIA_TYPES = {"image", "gif", "spin"}


def list_page_media(document_id: int, page_number: int) -> list[dict[str, object]]:
    with db() as conn:
        try:
            rows = conn.execute(
                """
                select id, media_type, title, alt_text, x_percent, y_percent,
                       width_percent, height_percent, is_published
                from manual_page_media
                where document_id = ? and page_number = ?
                order by id
                """,
                (document_id, page_number),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(row) for row in rows]


class AskRequest(BaseModel):
    question: str
    document_id: int | None = None


class AnalyzeRequest(BaseModel):
    document_id: int


class TranslateRequest(BaseModel):
    text: str
    target_language: str


class OcrRequest(BaseModel):
    replace_existing: bool = False


class ProductManualRequest(BaseModel):
    document_id: int
    product_slug: str
    display_name: str
    language: str = "ko"
    manufacturer: str | None = None
    model_group: str | None = None


class TranslationRequest(BaseModel):
    manual_version_id: int
    page_number: int
    target_language: str


class TranslationReviewRequest(BaseModel):
    manual_page_id: int
    target_language: str


def build_manual_knowledge_safely(document_id: int) -> dict[str, object]:
    try:
        return build_manual_knowledge(document_id)
    except OpenAIUnavailable as exc:
        with db() as conn:
            conn.execute(
                """
                insert into manual_knowledge_runs(document_id, status, message)
                values (?, ?, ?)
                """,
                (document_id, "unavailable", str(exc)),
            )
        return {"document_id": document_id, "status": "unavailable", "message": str(exc)}
    except Exception as exc:
        with db() as conn:
            conn.execute(
                """
                insert into manual_knowledge_runs(document_id, status, message)
                values (?, ?, ?)
                """,
                (document_id, "failed", str(exc)),
            )
        return {"document_id": document_id, "status": "failed", "message": str(exc)}


def process_manual_document_safely(document_id: int) -> dict[str, object]:
    try:
        correction = correct_document_pages(document_id)
        synced_pages = sync_manual_versions_from_document(document_id)
        knowledge = build_manual_knowledge(document_id)
        return {
            "document_id": document_id,
            "status": "completed",
            "correction": correction,
            "synced_pages": synced_pages,
            "knowledge": knowledge,
        }
    except Exception as exc:
        with db() as conn:
            conn.execute(
                """
                insert into manual_knowledge_runs(document_id, status, message)
                values (?, ?, ?)
                """,
                (document_id, "failed", str(exc)),
            )
        return {"document_id": document_id, "status": "failed", "message": str(exc)}


def create_english_version_safely(manual_version_id: int) -> dict[str, object]:
    try:
        return create_reviewed_translation_version(manual_version_id, target_language="en")
    except OpenAIUnavailable as exc:
        return {
            "source_manual_version_id": manual_version_id,
            "target_language": "en",
            "status": "unavailable",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "source_manual_version_id": manual_version_id,
            "target_language": "en",
            "status": "failed",
            "message": str(exc),
        }


def create_translation_version_safely(
    manual_version_id: int,
    target_language: str,
) -> dict[str, object]:
    try:
        return create_reviewed_translation_version(
            manual_version_id,
            target_language=target_language,
        )
    except Exception as exc:
        return {
            "source_manual_version_id": manual_version_id,
            "target_language": target_language,
            "status": "failed",
            "message": str(exc),
        }


def structure_manual_page(text: str) -> list[dict[str, object]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sections: list[dict[str, object]] = []
    paragraph: list[str] = []
    numbered_items: list[str] = []
    bullet_items: list[str] = []
    seen_headings: set[str] = set()

    def inline_markdown(value: str) -> str:
        escaped = html.escape(value)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        return escaped

    def flush_paragraph() -> None:
        if paragraph:
            value = " ".join(paragraph)
            sections.append({"type": "paragraph", "text": value, "html": inline_markdown(value)})
            paragraph.clear()

    def flush_items() -> None:
        if numbered_items:
            sections.append({"type": "list", "items": list(numbered_items)})
            numbered_items.clear()
        if bullet_items:
            sections.append({"type": "bullets", "items": list(bullet_items)})
            bullet_items.clear()

    for line in lines:
        markdown_image = re.fullmatch(r"!\[([^\]]*)\]\((/manual-media/\d+/\d+)\)", line)
        if markdown_image:
            flush_paragraph()
            flush_items()
            sections.append({
                "type": "image",
                "caption": markdown_image.group(1) or "Manual image",
                "url": markdown_image.group(2),
            })
            continue
        markdown_heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if markdown_heading:
            flush_paragraph()
            flush_items()
            sections.append({
                "type": "heading",
                "text": re.sub(r"\*\*", "", markdown_heading.group(2)).strip(),
                "level": 1 if len(markdown_heading.group(1)) == 1 else 2,
            })
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            if numbered_items:
                flush_items()
            bullet_items.append(bullet.group(1).strip())
            continue
        warning = re.match(
            r"^>\s*\*{0,2}(Warning|Caution|Note)(?::\*{0,2}|\*{1,2}:)?\s*(.*)$",
            line,
            re.I,
        )
        if warning:
            flush_paragraph()
            flush_items()
            sections.append({"type": "callout", "label": warning.group(1).upper(), "text": warning.group(2).strip()})
            continue
        if re.fullmatch(r"[A-Z]{2,}[A-Z0-9-]*\s+\d{1,4}", line):
            continue
        if line.lower() == "section":
            continue
        item = re.match(r"^\(?\d+\)?[.)]?\s+(.+)$", line)
        if item:
            flush_paragraph()
            if bullet_items:
                flush_items()
            numbered_items.append(item.group(1).strip())
            continue
        flush_items()
        if re.match(r"^(SMCS Code|Model|Part|Serial|Rating|Capacity|Voltage|Frequency)\s*:", line, re.I):
            flush_paragraph()
            label, value = line.split(":", 1)
            sections.append({"type": "fact", "label": label.strip(), "value": value.strip()})
            continue
        if re.match(r"^[a-z]\d{6,}\s+Illustration\s+\d+", line, re.I):
            flush_paragraph()
            sections.append({"type": "reference", "text": line})
            continue
        words = re.findall(r"[A-Za-z][A-Za-z0-9/-]*", line)
        title_like = bool(words) and sum(word[:1].isupper() for word in words) >= max(1, len(words) - 1)
        if len(line) <= 72 and title_like and not re.search(r"[.!?]$", line):
            flush_paragraph()
            normalized = re.sub(r"[^a-z0-9]", "", line.lower())
            if normalized in seen_headings:
                continue
            if normalized == "productinformation" and "productinformationsection" in seen_headings:
                continue
            seen_headings.add(normalized)
            sections.append({
                "type": "heading",
                "text": line,
                "level": 1 if line.lower().endswith("section") else 2,
            })
            continue
        if re.fullmatch(r"[A-Za-z]?\d{6,}[A-Za-z0-9-]*", line):
            continue
        paragraph.append(line)
    flush_paragraph()
    flush_items()
    return sections


def workspace_for_mode(mode: str) -> str:
    if mode == "spec":
        return "spec"
    if mode == "layout":
        return "layout"
    return "manual"


def suggest_product_identity(filename: str) -> tuple[str, str]:
    stem = Path(filename).stem
    words = [word for word in re.split(r"[_\-\s]+", stem) if word]
    useful = [
        word
        for word in words
        if not re.fullmatch(r"sample\d*", word, flags=re.IGNORECASE)
        and not re.fullmatch(r"\d+pages?", word, flags=re.IGNORECASE)
        and word.lower() not in {"manual", "user", "operation", "owners"}
    ]
    if not useful:
        useful = words or ["product"]
    display_name = " ".join(useful)
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-") or "product"
    return slug, display_name


def save_analysis_report(document_id: int, report_type: str, result: dict[str, object]) -> None:
    with db() as conn:
        conn.execute(
            """
            insert into analysis_reports(document_id, report_type, report, payload_json)
            values (?, ?, ?, ?)
            """,
            (
                document_id,
                report_type,
                str(result.get("report") or ""),
                json.dumps(result, ensure_ascii=False),
            ),
        )


def latest_analysis_report(document_id: int | None, report_type: str) -> dict[str, object] | None:
    if document_id is None:
        return None
    with db() as conn:
        row = conn.execute(
            """
            select payload_json, report, created_at
            from analysis_reports
            where document_id = ? and report_type = ?
            order by id desc
            limit 1
            """,
            (document_id, report_type),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {"report": row["report"]}
    payload["saved_at"] = row["created_at"]
    payload["saved"] = True
    return payload


def latest_knowledge_run(document_id: int | None) -> dict[str, object] | None:
    if document_id is None:
        return None
    with db() as conn:
        try:
            row = conn.execute(
                """
                select status, pages_processed, terms_count, faqs_count, message, created_at
                from manual_knowledge_runs
                where document_id = ?
                order by id desc
                limit 1
                """,
                (document_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return dict(row) if row else None


def load_workspace(
    document_id: int | None = None,
    page: int = 1,
    mode: str = "manual_admin",
    manual_version_id: int | None = None,
    view: str = "manual",
) -> dict[str, object]:
    valid_modes = {"manual_admin", "spec", "layout", "preview"}
    if mode not in valid_modes:
        mode = "manual_admin"
    document_workspace = workspace_for_mode(mode)
    with db() as conn:
        documents = conn.execute(
            """
            select d.id, d.filename, d.workspace, d.page_count, d.created_at, count(c.id) as chunk_count
            from documents d
            left join chunks c on c.document_id = d.id
            where d.workspace = ?
            group by d.id
            order by d.id desc
            """,
            (document_workspace,),
        ).fetchall()
        selected_document = None
        if documents:
            candidate_id = document_id or documents[0]["id"]
            selected_document = conn.execute(
                """
                select d.id, d.filename, d.workspace, d.page_count, d.created_at, count(c.id) as chunk_count
                from documents d
                left join chunks c on c.document_id = d.id
                where d.id = ? and d.workspace = ?
                group by d.id
                """,
                (candidate_id, document_workspace),
            ).fetchone()
            if selected_document is None:
                selected_document = conn.execute(
                    """
                    select d.id, d.filename, d.workspace, d.page_count, d.created_at, count(c.id) as chunk_count
                    from documents d
                    left join chunks c on c.document_id = d.id
                    where d.id = ?
                    group by d.id
                    """,
                    (documents[0]["id"],),
                ).fetchone()
        chunks = []
        page_chunks = []
        selected_page = 1
        previous_page = None
        next_page = None
        product_manuals = list_product_manuals()
        selected_manual = None
        selected_manual_page = None
        admin_manual = None
        admin_korean_translation = None
        selected_page_correction = None
        if selected_document:
            selected_page = max(1, min(page, selected_document["page_count"]))
            if selected_page > 1:
                previous_page = selected_page - 1
            if selected_page < selected_document["page_count"]:
                next_page = selected_page + 1
            chunks = conn.execute(
                """
                select page_number, section_title, content
                from chunks
                where document_id = ?
                order by page_number, id
                limit 30
                """,
                (selected_document["id"],),
            ).fetchall()
            page_chunks = conn.execute(
                """
                select page_number, section_title, content
                from chunks
                where document_id = ? and page_number = ?
                order by id
                """,
                (selected_document["id"], selected_page),
            ).fetchall()
            try:
                correction_row = conn.execute(
                    """
                    select corrected_text, correction_notes, uncertain_items, confidence, updated_at
                    from document_page_corrections
                    where document_id = ? and page_number = ?
                    """,
                    (selected_document["id"], selected_page),
                ).fetchone()
            except sqlite3.OperationalError:
                correction_row = None
            if correction_row:
                selected_page_correction = dict(correction_row)
                for field in ("correction_notes", "uncertain_items"):
                    try:
                        selected_page_correction[field] = json.loads(
                            selected_page_correction[field] or "[]"
                        )
                    except json.JSONDecodeError:
                        selected_page_correction[field] = []
            if mode == "manual_admin":
                admin_manual = conn.execute(
                    """
                    select
                      mv.id as manual_version_id,
                      mv.language,
                      mv.title,
                      mv.status as manual_status,
                      mv.source_document_id,
                      mv.product_family_id,
                      pf.slug,
                      pf.display_name
                    from manual_versions mv
                    join product_families pf on pf.id = mv.product_family_id
                    where mv.source_document_id = ?
                    order by case when mv.status = 'published_translation' then 1 else 0 end, mv.id
                    limit 1
                    """,
                    (selected_document["id"],),
                ).fetchone()
                if admin_manual:
                    translation = conn.execute(
                        """
                        select mpt.final_translation, mpt.status
                        from manual_versions mv
                        join manual_pages mp on mp.manual_version_id = mv.id
                        join manual_page_translations mpt on mpt.manual_page_id = mp.id
                        where mv.product_family_id = ? and mv.language = 'en'
                          and mp.page_number = ? and mpt.language = 'ko'
                          and coalesce(mpt.final_translation, '') <> ''
                        order by mpt.updated_at desc
                        limit 1
                        """,
                        (admin_manual["product_family_id"], selected_page),
                    ).fetchone()
                    if translation is None:
                        translation = conn.execute(
                            """
                            select mp.published_text as final_translation, mp.status
                            from manual_versions mv
                            join manual_pages mp on mp.manual_version_id = mv.id
                            where mv.product_family_id = ? and mv.language = 'ko'
                              and mp.page_number = ? and mp.status = 'published_translation'
                            order by mp.updated_at desc
                            limit 1
                            """,
                            (admin_manual["product_family_id"], selected_page),
                        ).fetchone()
                    if translation:
                        admin_korean_translation = dict(translation)
        if mode == "preview" and product_manuals:
            selected_manual_version_id = manual_version_id or int(product_manuals[0]["manual_version_id"])
            selected_manual = conn.execute(
                """
                select
                  mv.id as manual_version_id,
                  mv.language,
                  mv.title,
                  mv.status as manual_status,
                  mv.source_document_id,
                  pf.slug,
                  pf.display_name,
                  pf.manufacturer,
                  pf.model_group,
                  d.filename as source_filename,
                  coalesce(count(mp.id), 0) as page_count
                from manual_versions mv
                join product_families pf on pf.id = mv.product_family_id
                left join documents d on d.id = mv.source_document_id
                left join manual_pages mp on mp.manual_version_id = mv.id
                where mv.id = ?
                group by mv.id
                """,
                (selected_manual_version_id,),
            ).fetchone()
            if selected_manual:
                selected_page = max(1, min(page, int(selected_manual["page_count"] or 1)))
                previous_page = selected_page - 1 if selected_page > 1 else None
                next_page = (
                    selected_page + 1
                    if selected_page < int(selected_manual["page_count"] or 1)
                    else None
                )
                selected_manual_page = conn.execute(
                    """
                    select *
                    from manual_pages
                    where manual_version_id = ? and page_number = ?
                    """,
                    (selected_manual_version_id, selected_page),
                ).fetchone()
    admin_page_blocks = (
        list_manual_page_blocks(int(admin_manual["manual_version_id"]), selected_page)
        if admin_manual
        else []
    )
    preview_page_blocks = (
        list_manual_page_blocks(int(selected_manual["manual_version_id"]), selected_page)
        if selected_manual
        else []
    )
    preview_sections = []
    if selected_manual_page:
        preview_text = (
            selected_manual_page["published_text"]
            or selected_manual_page["ai_corrected_text"]
            or selected_manual_page["raw_ocr_text"]
            or ""
        )
        preview_sections = structure_manual_page(preview_text)
    media_document_id = None
    if selected_document:
        media_document_id = int(selected_document["id"])
    elif selected_manual and selected_manual["source_document_id"]:
        media_document_id = int(selected_manual["source_document_id"])
    page_media = (
        list_page_media(media_document_id, selected_page)
        if media_document_id
        else []
    )
    suggested_product_slug = ""
    suggested_display_name = ""
    suggested_source_language = "ko"
    if selected_document:
        suggested_product_slug, suggested_display_name = suggest_product_identity(
            selected_document["filename"]
        )
        sample_text = " ".join(str(chunk["content"]) for chunk in chunks[:5])
        latin_count = len(re.findall(r"[A-Za-z]", sample_text))
        korean_count = len(re.findall(r"[가-힣]", sample_text))
        if latin_count > max(30, korean_count * 2):
            suggested_source_language = "en"
    if admin_manual:
        suggested_product_slug = admin_manual["slug"]
        suggested_display_name = admin_manual["display_name"]
    return {
        "mode": mode,
        "view": view if view in {"manual", "original"} else "manual",
        "documents": documents,
        "selected_document": selected_document,
        "selected_page": selected_page,
        "previous_page": previous_page,
        "next_page": next_page,
        "chunks": chunks,
        "page_chunks": page_chunks,
        "product_manuals": product_manuals,
        "selected_manual": selected_manual,
        "selected_manual_page": selected_manual_page,
        "selected_page_correction": selected_page_correction,
        "admin_manual": admin_manual,
        "admin_korean_translation": admin_korean_translation,
        "admin_page_blocks": admin_page_blocks,
        "preview_page_blocks": preview_page_blocks,
        "preview_sections": preview_sections,
        "page_media": page_media,
        "suggested_product_slug": suggested_product_slug,
        "suggested_display_name": suggested_display_name,
        "suggested_source_language": suggested_source_language,
        "knowledge_run": latest_knowledge_run(
            selected_document["id"] if selected_document else (
                selected_manual["source_document_id"] if selected_manual else None
            )
        ),
        "saved_spec_result": latest_analysis_report(
            selected_document["id"] if selected_document and mode == "spec" else None,
            "spec",
        ),
        "saved_layout_result": latest_analysis_report(
            selected_document["id"] if selected_document and mode == "layout" else None,
            "layout",
        ),
    }


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    document_id: int | None = None,
    page: int = 1,
    mode: str = "manual_admin",
    manual_version_id: int | None = None,
    view: str = "manual",
) -> HTMLResponse:
    workspace = load_workspace(document_id, page, mode, manual_version_id, view)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "app_name": settings.app_name, **workspace},
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/documents/{document_id}/pages/{page_number}.png")
def document_page_image(document_id: int, page_number: int) -> Response:
    try:
        image = render_page_png(document_id, page_number)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(content=image, media_type="image/png")


@app.get("/api/manual-media/{media_id}")
def manual_media_info(media_id: int) -> dict[str, object]:
    with db() as conn:
        row = conn.execute(
            "select id, media_type, title, alt_text, files_json from manual_page_media where id = ? and is_published = 1",
            (media_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Manual media was not found.")
    filenames = json.loads(row["files_json"] or "[]")
    return {
        "id": row["id"],
        "media_type": row["media_type"],
        "title": row["title"],
        "alt_text": row["alt_text"] or row["title"],
        "files": [f"/manual-media/{media_id}/{index}" for index in range(len(filenames))],
    }


@app.get("/manual-media/{media_id}/{file_index}")
def manual_media_file(media_id: int, file_index: int) -> FileResponse:
    with db() as conn:
        row = conn.execute(
            "select files_json from manual_page_media where id = ? and is_published = 1",
            (media_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Manual media was not found.")
    filenames = json.loads(row["files_json"] or "[]")
    if file_index < 0 or file_index >= len(filenames):
        raise HTTPException(status_code=404, detail="Media file was not found.")
    media_root = settings.manual_media_dir.resolve()
    path = (media_root / filenames[file_index]).resolve()
    if path.parent != media_root or not path.exists():
        raise HTTPException(status_code=404, detail="Media file was not found.")
    return FileResponse(path)


@app.get("/api/documents")
def list_documents(workspace: str = "manual") -> list[dict[str, object]]:
    if workspace not in {"manual", "spec", "layout"}:
        workspace = "manual"
    with db() as conn:
        rows = conn.execute(
            """
            select d.id, d.filename, d.workspace, d.page_count, d.created_at, count(c.id) as chunk_count
            from documents d
            left join chunks c on c.document_id = d.id
            where d.workspace = ?
            group by d.id
            order by d.id desc
            """,
            (workspace,),
        ).fetchall()
    documents = []
    for row in rows:
        item = dict(row)
        item["indexing_status"] = "needs_ocr" if item["chunk_count"] == 0 else "indexed"
        documents.append(item)
    return documents


@app.post("/api/documents")
async def upload_document(
    file: UploadFile = File(...),
    workspace: str = Form("manual"),
) -> dict[str, object]:
    if workspace not in {"manual", "spec", "layout"}:
        workspace = "manual"
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported in MVP.")
    with NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        temp.write(await file.read())
        temp_path = Path(temp.name)
    try:
        document_id = ingest_pdf(temp_path, display_name=file.filename, workspace=workspace)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return {"document_id": document_id, "filename": file.filename, "workspace": workspace}


@app.post("/upload-form", response_class=HTMLResponse)
async def upload_form(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("manual_admin"),
) -> HTMLResponse:
    workspace_name = workspace_for_mode(mode)
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported in MVP.")
    with NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        temp.write(await file.read())
        temp_path = Path(temp.name)
    try:
        document_id = ingest_pdf(
            temp_path,
            display_name=file.filename,
            workspace=workspace_name,
        )
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    workspace = load_workspace(document_id, mode=mode)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": f"Uploaded {file.filename}",
            **workspace,
        },
    )


@app.post("/save-ocr-page-form", response_class=HTMLResponse)
def save_ocr_page_form(
    request: Request,
    background_tasks: BackgroundTasks,
    document_id: int = Form(...),
    page: int = Form(1),
    corrected_text: str = Form(""),
) -> HTMLResponse:
    text = corrected_text.strip()
    with db() as conn:
        document = conn.execute(
            "select id from documents where id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        conn.execute(
            "delete from chunks where document_id = ? and page_number = ?",
            (document_id, page),
        )
        if text:
            conn.execute(
                """
                insert into chunks(document_id, page_number, section_title, content)
                values (?, ?, ?, ?)
                """,
                (document_id, page, f"Page {page}", text),
            )
        conn.execute(
            """
            update manual_pages
            set raw_ocr_text = ?, ai_corrected_text = ?, published_text = ?,
                status = 'edited', updated_at = current_timestamp
            where page_number = ?
              and manual_version_id in (
                select id from manual_versions where source_document_id = ?
              )
            """,
            (text, text, text, page, document_id),
        )
        conn.execute(
            """
            update document_page_corrections
            set corrected_text = ?, updated_at = current_timestamp
            where document_id = ? and page_number = ?
            """,
            (text, document_id, page),
        )
    background_tasks.add_task(build_manual_knowledge_safely, document_id)
    workspace = load_workspace(document_id, page, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": f"Saved OCR text for page {page}.",
            **workspace,
        },
    )


@app.post("/save-review-ocr-form", response_class=HTMLResponse)
def save_review_ocr_form(
    request: Request,
    document_id: int = Form(...),
    page: int = Form(1),
    mode: str = Form("spec"),
    corrected_text: str = Form(""),
) -> HTMLResponse:
    if mode != "spec":
        raise HTTPException(status_code=400, detail="OCR editing is only available in Spec Sheet Review.")
    text = corrected_text.strip()
    with db() as conn:
        document = conn.execute(
            "select workspace from documents where id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        if document["workspace"] != "spec":
            raise HTTPException(status_code=409, detail="Document belongs to another workspace.")
        old_rows = conn.execute(
            "select content from chunks where document_id = ? and page_number = ? order by id",
            (document_id, page),
        ).fetchall()
        raw_text = "\n\n".join(row["content"] for row in old_rows)
        conn.execute(
            "delete from chunks where document_id = ? and page_number = ?",
            (document_id, page),
        )
        if text:
            conn.executemany(
                """
                insert into chunks(document_id, page_number, section_title, content)
                values (?, ?, ?, ?)
                """,
                [
                    (document_id, chunk.page_number, chunk.section_title, chunk.content)
                    for chunk in split_chunks(text, page)
                ],
            )
        conn.execute(
            """
            insert into document_page_corrections(
              document_id, page_number, raw_text, corrected_text, confidence
            ) values (?, ?, ?, ?, 'human_reviewed')
            on conflict(document_id, page_number) do update set
              corrected_text = excluded.corrected_text,
              confidence = 'human_reviewed',
              updated_at = current_timestamp
            """,
            (document_id, page, raw_text or text, text),
        )
        conn.execute(
            "delete from analysis_reports where document_id = ? and report_type = 'spec'",
            (document_id,),
        )
    workspace = load_workspace(document_id, page, mode="spec")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": "OCR 수정본을 저장했습니다. 정밀 분석을 다시 실행하면 수정된 수치가 반영됩니다.",
            **workspace,
        },
    )


def run_layout_review_safely(document_id: int) -> dict[str, object]:
    try:
        return review_layout(document_id)
    except OpenAIUnavailable as exc:
        return {
            "document_id": document_id,
            "notice": "부분 OCR은 저장했지만 AI 정밀 분석을 실행할 수 없습니다.",
            "report": str(exc),
            "checks": [],
            "engine": "unavailable",
        }


@app.post("/layout-region-ocr-form", response_class=HTMLResponse)
def layout_region_ocr_form(
    request: Request,
    document_id: int = Form(...),
    page: int = Form(1),
    x_percent: float = Form(...),
    y_percent: float = Form(...),
    width_percent: float = Form(...),
    height_percent: float = Form(...),
) -> HTMLResponse:
    with db() as conn:
        document = conn.execute(
            "select workspace from documents where id = ?",
            (document_id,),
        ).fetchone()
    if document is None:
        raise HTTPException(status_code=404, detail="Document was not found.")
    if document["workspace"] != "layout":
        raise HTTPException(status_code=409, detail="Document belongs to another workspace.")
    try:
        text = ocr_page_region(
            document_id,
            page,
            x_percent,
            y_percent,
            width_percent,
            height_percent,
        )
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise HTTPException(status_code=409, detail=f"선택 영역을 판독하지 못했습니다: {exc}") from exc

    chunk_id = None
    layout_result = None
    if text:
        with db() as conn:
            cursor = conn.execute(
                """
                insert into chunks(document_id, page_number, section_title, content)
                values (?, ?, ?, ?)
                """,
                (
                    document_id,
                    page,
                    f"Layout Region OCR ({x_percent:.1f}, {y_percent:.1f}, {width_percent:.1f}, {height_percent:.1f})",
                    text,
                ),
            )
            chunk_id = int(cursor.lastrowid)
        layout_result = run_layout_review_safely(document_id)
        save_analysis_report(document_id, "layout", layout_result)
    workspace = load_workspace(document_id, page, mode="layout")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "layout_region_result": {
                "chunk_id": chunk_id,
                "text": text,
                "has_text": bool(text),
            },
            "layout_result": layout_result,
            "upload_message": "선택 영역을 고해상도로 다시 판독하고 분석에 반영했습니다." if text else "선택 영역에서 글자를 찾지 못했습니다.",
            **workspace,
        },
    )


@app.post("/layout-region-save-form", response_class=HTMLResponse)
def layout_region_save_form(
    request: Request,
    document_id: int = Form(...),
    page: int = Form(1),
    chunk_id: int = Form(...),
    corrected_text: str = Form(""),
) -> HTMLResponse:
    text = corrected_text.strip()
    with db() as conn:
        chunk = conn.execute(
            """
            select c.id
            from chunks c join documents d on d.id = c.document_id
            where c.id = ? and c.document_id = ? and c.page_number = ?
              and d.workspace = 'layout' and c.section_title like 'Layout Region OCR%'
            """,
            (chunk_id, document_id, page),
        ).fetchone()
        if chunk is None:
            raise HTTPException(status_code=404, detail="Region OCR result was not found.")
        conn.execute("update chunks set content = ? where id = ?", (text, chunk_id))
    layout_result = run_layout_review_safely(document_id)
    save_analysis_report(document_id, "layout", layout_result)
    workspace = load_workspace(document_id, page, mode="layout")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "layout_region_result": {
                "chunk_id": chunk_id,
                "text": text,
                "has_text": bool(text),
            },
            "layout_result": layout_result,
            "upload_message": "부분 판독 수정본을 저장하고 레이아웃 분석을 다시 실행했습니다.",
            **workspace,
        },
    )


@app.post("/manual-media-upload-form", response_class=HTMLResponse)
async def manual_media_upload_form(
    request: Request,
    document_id: int = Form(...),
    page: int = Form(1),
    media_type: str = Form(...),
    title: str = Form(...),
    alt_text: str = Form(""),
    x_percent: float = Form(...),
    y_percent: float = Form(...),
    width_percent: float = Form(...),
    height_percent: float = Form(...),
    files: list[UploadFile] = File(...),
) -> HTMLResponse:
    if media_type not in MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported media type.")
    if not files or (media_type in {"image", "gif"} and len(files) != 1):
        raise HTTPException(status_code=400, detail="Select one file, or multiple frames for a 360 spin.")
    if media_type == "spin" and len(files) > 72:
        raise HTTPException(status_code=400, detail="A 360 spin supports up to 72 frames.")
    if not (0 <= x_percent <= 100 and 0 <= y_percent <= 100):
        raise HTTPException(status_code=400, detail="Invalid hotspot position.")
    if not (0.5 <= width_percent <= 100 and 0.5 <= height_percent <= 100):
        raise HTTPException(status_code=400, detail="Invalid hotspot size.")
    if x_percent + width_percent > 100.5 or y_percent + height_percent > 100.5:
        raise HTTPException(status_code=400, detail="Hotspot must stay inside the page image.")

    stored_filenames: list[str] = []
    total_bytes = 0
    settings.manual_media_dir.mkdir(parents=True, exist_ok=True)
    try:
        for upload in files:
            suffix = Path(upload.filename or "").suffix.lower()
            if suffix not in MEDIA_EXTENSIONS:
                raise HTTPException(status_code=400, detail=f"Unsupported image type: {suffix or 'unknown'}")
            content = await upload.read()
            total_bytes += len(content)
            if len(content) > 25 * 1024 * 1024 or total_bytes > 150 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Media upload is too large.")
            filename = f"{uuid4().hex}{suffix}"
            (settings.manual_media_dir / filename).write_bytes(content)
            stored_filenames.append(filename)
        with db() as conn:
            conn.execute(
                """
                insert into manual_page_media(
                  document_id, page_number, media_type, title, alt_text, files_json,
                  x_percent, y_percent, width_percent, height_percent
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    page,
                    media_type,
                    title.strip() or "Interactive media",
                    alt_text.strip(),
                    json.dumps(stored_filenames),
                    x_percent,
                    y_percent,
                    width_percent,
                    height_percent,
                ),
            )
    except Exception:
        for filename in stored_filenames:
            (settings.manual_media_dir / filename).unlink(missing_ok=True)
        raise

    workspace = load_workspace(document_id, page, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": "Interactive media added to this page.",
            **workspace,
        },
    )


def safe_chunk_upload_dir(upload_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload ID.")
    chunk_root = (settings.manual_media_dir / ".chunks").resolve()
    upload_dir = (chunk_root / upload_id).resolve()
    if upload_dir.parent != chunk_root:
        raise HTTPException(status_code=400, detail="Invalid upload path.")
    return upload_dir


@app.post("/api/manual-media/chunk")
async def upload_manual_media_chunk(
    upload_id: str = Form(...),
    file_index: int = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    chunk: UploadFile = File(...),
) -> dict[str, object]:
    if not (0 <= file_index < 72 and 0 <= chunk_index < total_chunks <= 64):
        raise HTTPException(status_code=400, detail="Invalid chunk metadata.")
    content = await chunk.read()
    if not content or len(content) > 1024 * 1024:
        raise HTTPException(status_code=413, detail="Chunk must be 1MB or smaller.")
    upload_dir = safe_chunk_upload_dir(upload_id)
    file_dir = upload_dir / str(file_index)
    file_dir.mkdir(parents=True, exist_ok=True)
    (file_dir / f"{chunk_index:04d}.part").write_bytes(content)
    return {"status": "stored", "chunk_index": chunk_index}


@app.post("/api/manual-media/finalize")
def finalize_manual_media_chunks(
    document_id: int = Form(...),
    page: int = Form(1),
    media_type: str = Form(...),
    title: str = Form(...),
    alt_text: str = Form(""),
    x_percent: float = Form(...),
    y_percent: float = Form(...),
    width_percent: float = Form(...),
    height_percent: float = Form(...),
    upload_id: str = Form(...),
    file_manifest: str = Form(...),
) -> dict[str, object]:
    if media_type not in MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported media type.")
    if not (0 <= x_percent <= 100 and 0 <= y_percent <= 100):
        raise HTTPException(status_code=400, detail="Invalid hotspot position.")
    if not (0.5 <= width_percent <= 100 and 0.5 <= height_percent <= 100):
        raise HTTPException(status_code=400, detail="Invalid hotspot size.")
    try:
        manifest = json.loads(file_manifest)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid file manifest.") from exc
    if not isinstance(manifest, list) or not manifest or len(manifest) > 72:
        raise HTTPException(status_code=400, detail="Invalid media file count.")
    if media_type in {"image", "gif"} and len(manifest) != 1:
        raise HTTPException(status_code=400, detail="Select one image or GIF.")

    upload_dir = safe_chunk_upload_dir(upload_id)
    stored_filenames: list[str] = []
    total_bytes = 0
    try:
        for file_index, item in enumerate(manifest):
            original_name = str(item.get("name") or "")
            suffix = Path(original_name).suffix.lower()
            chunks_count = int(item.get("chunks") or 0)
            expected_size = int(item.get("size") or 0)
            if suffix not in MEDIA_EXTENSIONS or not (1 <= chunks_count <= 64):
                raise HTTPException(status_code=400, detail="Invalid media file metadata.")
            if expected_size <= 0 or expected_size > 25 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Each media file must be 25MB or smaller.")
            destination_name = f"{uuid4().hex}{suffix}"
            destination = settings.manual_media_dir / destination_name
            written = 0
            with destination.open("wb") as output:
                for chunk_index in range(chunks_count):
                    part = upload_dir / str(file_index) / f"{chunk_index:04d}.part"
                    if not part.exists():
                        raise HTTPException(status_code=409, detail="An upload chunk is missing.")
                    data = part.read_bytes()
                    output.write(data)
                    written += len(data)
            if written != expected_size:
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=409, detail="Uploaded file size mismatch.")
            total_bytes += written
            if total_bytes > 150 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Total media upload must be 150MB or smaller.")
            stored_filenames.append(destination_name)

        with db() as conn:
            cur = conn.execute(
                """
                insert into manual_page_media(
                  document_id, page_number, media_type, title, alt_text, files_json,
                  x_percent, y_percent, width_percent, height_percent
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id, page, media_type, title.strip() or "Interactive media",
                    alt_text.strip(), json.dumps(stored_filenames), x_percent, y_percent,
                    width_percent, height_percent,
                ),
            )
            media_id = int(cur.lastrowid)
    except Exception:
        for filename in stored_filenames:
            (settings.manual_media_dir / filename).unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)
    return {"status": "created", "media_id": media_id}


@app.post("/manual-media-delete-form", response_class=HTMLResponse)
def manual_media_delete_form(
    request: Request,
    media_id: int = Form(...),
    document_id: int = Form(...),
    page: int = Form(1),
) -> HTMLResponse:
    with db() as conn:
        row = conn.execute(
            "select files_json from manual_page_media where id = ? and document_id = ?",
            (media_id, document_id),
        ).fetchone()
        conn.execute(
            "delete from manual_page_media where id = ? and document_id = ?",
            (media_id, document_id),
        )
    if row:
        for filename in json.loads(row["files_json"] or "[]"):
            path = (settings.manual_media_dir / filename).resolve()
            if path.parent == settings.manual_media_dir.resolve():
                path.unlink(missing_ok=True)
    workspace = load_workspace(document_id, page, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": "Interactive media removed.",
            **workspace,
        },
    )


@app.post("/manual-block-add-form", response_class=HTMLResponse)
def manual_block_add_form(
    request: Request,
    manual_version_id: int = Form(...),
    document_id: int = Form(...),
    page: int = Form(1),
    block_type: str = Form(...),
    content: str = Form(""),
    asset_url: str = Form(""),
    caption: str = Form(""),
) -> HTMLResponse:
    add_manual_page_block(
        manual_version_id,
        page,
        block_type,
        content=content,
        asset_url=asset_url,
        caption=caption,
    )
    workspace = load_workspace(document_id, page, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": "Content block added.",
            **workspace,
        },
    )


@app.post("/translate-page-ko-form", response_class=HTMLResponse)
def translate_page_ko_form(
    request: Request,
    document_id: int = Form(...),
    page: int = Form(1),
) -> HTMLResponse:
    with db() as conn:
        source = conn.execute(
            """
            select mv.id
            from manual_versions mv
            where mv.source_document_id = ? and mv.language = 'en'
            order by case when mv.status = 'published_translation' then 1 else 0 end, mv.id
            limit 1
            """,
            (document_id,),
        ).fetchone()
    message = "영어 매뉴얼을 먼저 등록해 주세요."
    if source:
        try:
            create_reviewed_page_translation(int(source["id"]), page, "ko")
            message = f"Page {page} 한국어 번역본을 생성했습니다."
        except Exception as exc:
            message = f"한국어 번역을 완료하지 못했습니다: {exc}"
    workspace = load_workspace(document_id, page, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": message,
            **workspace,
        },
    )


@app.post("/manual-block-update-form", response_class=HTMLResponse)
def manual_block_update_form(
    request: Request,
    block_id: int = Form(...),
    document_id: int = Form(...),
    page: int = Form(1),
    block_type: str = Form(...),
    content: str = Form(""),
    asset_url: str = Form(""),
    caption: str = Form(""),
    status: str = Form("draft"),
) -> HTMLResponse:
    update_manual_page_block(
        block_id,
        block_type,
        content,
        asset_url,
        caption,
        status,
    )
    workspace = load_workspace(document_id, page, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": "Content block saved.",
            **workspace,
        },
    )


@app.post("/manual-block-delete-form", response_class=HTMLResponse)
def manual_block_delete_form(
    request: Request,
    block_id: int = Form(...),
    document_id: int = Form(...),
    page: int = Form(1),
) -> HTMLResponse:
    delete_manual_page_block(block_id)
    workspace = load_workspace(document_id, page, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": "Content block removed.",
            **workspace,
        },
    )


@app.post("/product-manual-form", response_class=HTMLResponse)
def product_manual_form(
    request: Request,
    background_tasks: BackgroundTasks,
    document_id: int = Form(...),
    product_slug: str = Form(...),
    display_name: str = Form(...),
    language: str = Form("ko"),
    manufacturer: str | None = Form(None),
    model_group: str | None = Form(None),
) -> HTMLResponse:
    manual_version_id = create_manual_version_from_document(
        document_id,
        product_slug.strip(),
        display_name.strip(),
        language=language.strip() or "ko",
        manufacturer=manufacturer.strip() if manufacturer else None,
        model_group=model_group.strip() if model_group else None,
    )
    source_language = (language.strip() or "ko").lower()
    background_tasks.add_task(process_manual_document_safely, document_id)
    if source_language == "en":
        background_tasks.add_task(create_translation_version_safely, manual_version_id, "ko")
    elif source_language != "en":
        background_tasks.add_task(create_translation_version_safely, manual_version_id, "en")
    workspace = load_workspace(
        document_id,
        mode="preview",
        manual_version_id=manual_version_id,
    )
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": f"Registered {display_name} as a product manual.",
            **workspace,
        },
    )


@app.post("/delete-document-form", response_class=HTMLResponse)
def delete_document_form(request: Request, document_id: int = Form(...)) -> HTMLResponse:
    with db() as conn:
        row = conn.execute(
            "select stored_path from documents where id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        stored_path = Path(row["stored_path"])
        conn.execute("delete from documents where id = ?", (document_id,))
    try:
        stored_path.relative_to(settings.uploads_dir)
        stored_path.unlink(missing_ok=True)
    except ValueError:
        pass
    workspace = load_workspace(mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": "Deleted uploaded PDF.",
            **workspace,
        },
    )


@app.post("/delete-review-document-form", response_class=HTMLResponse)
def delete_review_document_form(
    request: Request,
    document_id: int = Form(...),
    mode: str = Form(...),
) -> HTMLResponse:
    if mode not in {"spec", "layout"}:
        raise HTTPException(status_code=400, detail="Invalid review workspace.")
    expected_workspace = workspace_for_mode(mode)
    with db() as conn:
        row = conn.execute(
            """
            select filename, stored_path, workspace
            from documents
            where id = ?
            """,
            (document_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        if row["workspace"] != expected_workspace:
            raise HTTPException(status_code=409, detail="Document belongs to another workspace.")
        stored_path = Path(row["stored_path"])
        filename = row["filename"]
        conn.execute("delete from documents where id = ?", (document_id,))
        remaining_references = conn.execute(
            "select count(*) from documents where stored_path = ?",
            (str(stored_path),),
        ).fetchone()[0]
    if not remaining_references:
        try:
            stored_path.resolve().relative_to(settings.uploads_dir.resolve())
            stored_path.unlink(missing_ok=True)
        except ValueError:
            pass
    workspace = load_workspace(mode=mode)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": f"Deleted {filename} from the review workspace.",
            **workspace,
        },
    )


@app.post("/delete-product-family-form", response_class=HTMLResponse)
def delete_product_family_form(
    request: Request,
    product_family_id: int = Form(...),
    document_id: int | None = Form(None),
) -> HTMLResponse:
    with db() as conn:
        product = conn.execute(
            "select display_name from product_families where id = ?",
            (product_family_id,),
        ).fetchone()
        conn.execute("delete from product_families where id = ?", (product_family_id,))
    workspace = load_workspace(document_id, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": (
                f"Removed {product['display_name']} from Preview."
                if product
                else "The product manual was already removed."
            ),
            **workspace,
        },
    )


@app.post("/api/documents/{document_id}/ocr")
def ocr_document(
    document_id: int,
    background_tasks: BackgroundTasks,
    payload: OcrRequest | None = None,
) -> dict[str, object]:
    try:
        result = run_ocr_for_document(
            document_id,
            replace_existing=payload.replace_existing if payload else False,
        )
        if result.get("chunks_added", 0) or result.get("status") == "ocr_completed":
            background_tasks.add_task(process_manual_document_safely, document_id)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=exc.stderr or str(exc)) from exc


@app.post("/api/product-manuals/from-document")
def product_manual_from_document(
    payload: ProductManualRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    try:
        manual_version_id = create_manual_version_from_document(
            payload.document_id,
            payload.product_slug,
            payload.display_name,
            language=payload.language,
            manufacturer=payload.manufacturer,
            model_group=payload.model_group,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    source_language = payload.language.lower()
    background_tasks.add_task(process_manual_document_safely, payload.document_id)
    if source_language == "en":
        background_tasks.add_task(create_translation_version_safely, manual_version_id, "ko")
    elif source_language != "en":
        background_tasks.add_task(create_translation_version_safely, manual_version_id, "en")
    return {
        "manual_version_id": manual_version_id,
        "product_slug": payload.product_slug,
        "language": payload.language,
        "english_version": "queued" if payload.language.lower() != "en" else "source_is_english",
    }


@app.post("/api/translations/draft")
def translation_draft(payload: TranslationRequest) -> dict[str, object]:
    try:
        return generate_translation_draft(
            payload.manual_version_id,
            payload.page_number,
            payload.target_language,
        )
    except OpenAIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/manual-versions/{manual_version_id}/english")
def create_english_manual_version(manual_version_id: int) -> dict[str, object]:
    result = create_english_version_safely(manual_version_id)
    if result.get("status") in {"failed", "unavailable"}:
        raise HTTPException(status_code=503, detail=result)
    return result


@app.post("/api/translations/check-accuracy")
def translation_accuracy(payload: TranslationReviewRequest) -> dict[str, object]:
    try:
        return check_translation_accuracy(payload.manual_page_id, payload.target_language)
    except OpenAIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/translations/native-review")
def translation_native_review(payload: TranslationReviewRequest) -> dict[str, object]:
    try:
        return native_review_translation(payload.manual_page_id, payload.target_language)
    except OpenAIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/ask")
def ask(payload: AskRequest) -> dict[str, object]:
    if not payload.question.strip():
        raise HTTPException(status_code=400, detail="Question is required.")
    return answer_question(payload.question, payload.document_id)


@app.post("/api/spec/analyze")
def spec_analyze(payload: AnalyzeRequest) -> dict[str, object]:
    try:
        result = analyze_spec(payload.document_id)
        save_analysis_report(payload.document_id, "spec", result)
        return result
    except OpenAIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/layout/review")
def layout_review(payload: AnalyzeRequest) -> dict[str, object]:
    try:
        result = review_layout(payload.document_id)
        save_analysis_report(payload.document_id, "layout", result)
        return result
    except OpenAIUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/spec-review-form", response_class=HTMLResponse)
def spec_review_form(
    request: Request,
    document_id: int = Form(...),
    page: int = Form(1),
) -> HTMLResponse:
    try:
        result = analyze_spec(document_id)
    except OpenAIUnavailable as exc:
        result = {
            "document_id": document_id,
            "report": f"AI 정밀 분석을 실행할 수 없습니다: {exc}",
            "fields": {},
            "evidence": [],
            "engine": "unavailable",
        }
    save_analysis_report(document_id, "spec", result)
    workspace = load_workspace(document_id, page, mode="spec")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "spec_result": result,
            **workspace,
        },
    )


@app.post("/layout-review-form", response_class=HTMLResponse)
def layout_review_form(
    request: Request,
    document_id: int = Form(...),
    page: int = Form(1),
) -> HTMLResponse:
    try:
        result = review_layout(document_id)
    except OpenAIUnavailable as exc:
        result = {
            "document_id": document_id,
            "notice": "AI 정밀 분석을 실행할 수 없습니다.",
            "report": str(exc),
            "checks": [],
            "engine": "unavailable",
        }
    save_analysis_report(document_id, "layout", result)
    workspace = load_workspace(document_id, page, mode="layout")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "layout_result": result,
            **workspace,
        },
    )


@app.post("/api/translate")
def translate(payload: TranslateRequest) -> dict[str, str]:
    if payload.target_language not in {"English", "Japanese", "Chinese", "Arabic", "Korean"}:
        raise HTTPException(status_code=400, detail="Unsupported target language.")
    return translation_workflow(payload.text, payload.target_language)


@app.post("/api/documents/{document_id}/knowledge")
def rebuild_document_knowledge(document_id: int) -> dict[str, object]:
    return build_manual_knowledge_safely(document_id)


@app.post("/ask-form", response_class=HTMLResponse)
def ask_form(
    request: Request,
    question: str = Form(...),
    document_id: int | None = Form(None),
    page: int = Form(1),
    mode: str = Form("manual_admin"),
    manual_version_id: int | None = Form(None),
    view: str = Form("manual"),
) -> HTMLResponse:
    try:
        if mode == "preview":
            language = "ko"
            if manual_version_id is not None:
                with db() as conn:
                    row = conn.execute(
                        "select language from manual_versions where id = ?",
                        (manual_version_id,),
                    ).fetchone()
                if row:
                    language = row["language"]
            result = answer_question_with_ai(question, document_id, language=language)
        else:
            result = answer_question(question, document_id)
    except OpenAIUnavailable as exc:
        result = {
            "answer": f"AI 답변 엔진을 사용할 수 없습니다: {exc}",
            "evidence": [],
            "needs_ocr": False,
        }
    workspace = load_workspace(document_id, page, mode, manual_version_id, view)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "question": question,
            "result": result,
            **workspace,
        },
    )


@app.post("/quick-form", response_class=HTMLResponse)
def quick_form(
    request: Request,
    action: str = Form(...),
    document_id: int = Form(...),
    page: int = Form(1),
    mode: str = Form("manual_admin"),
    manual_version_id: int | None = Form(None),
    view: str = Form("manual"),
) -> HTMLResponse:
    workspace = load_workspace(document_id, page, mode, manual_version_id, view)
    selected_document = workspace["selected_document"]
    selected_manual_page = workspace["selected_manual_page"]
    if selected_document is None:
        raise HTTPException(status_code=404, detail="Document was not found.")
    if mode == "preview" and selected_manual_page is not None:
        page_text = (
            selected_manual_page["published_text"]
            or selected_manual_page["ai_corrected_text"]
            or selected_manual_page["raw_ocr_text"]
            or ""
        )
        display_name = workspace["selected_manual"]["display_name"]
    else:
        page_chunks = workspace["page_chunks"]
        page_text = "\n\n".join(chunk["content"] for chunk in page_chunks)
        display_name = selected_document["filename"]
    result = page_action(
        action,
        page_text,
        display_name,
        workspace["selected_page"],
    )
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "quick_result": result,
            **workspace,
        },
    )


@app.post("/ocr-form", response_class=HTMLResponse)
def ocr_form(
    request: Request,
    background_tasks: BackgroundTasks,
    document_id: int = Form(...),
    mode: str = Form("manual_admin"),
) -> HTMLResponse:
    result = run_ocr_for_document(document_id)
    if result.get("chunks_added", 0) or result.get("status") == "ocr_completed":
        background_tasks.add_task(process_manual_document_safely, document_id)
    workspace = load_workspace(document_id, mode=mode)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "ocr_result": result,
            **workspace,
        },
    )


@app.post("/process-document-form", response_class=HTMLResponse)
def process_document_form(
    request: Request,
    background_tasks: BackgroundTasks,
    document_id: int = Form(...),
) -> HTMLResponse:
    background_tasks.add_task(process_manual_document_safely, document_id)
    workspace = load_workspace(document_id, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": "AI source correction and knowledge preparation queued.",
            **workspace,
        },
    )
