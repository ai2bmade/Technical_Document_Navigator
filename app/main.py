from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
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
from app.ocr import run_ocr_for_document
from app.page_images import render_page_png
from app.pdf_ingest import ingest_pdf
from app.storage import db, init_db


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


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

    def flush_paragraph() -> None:
        if paragraph:
            sections.append({"type": "paragraph", "text": " ".join(paragraph)})
            paragraph.clear()

    def flush_items() -> None:
        if numbered_items:
            sections.append({"type": "list", "items": list(numbered_items)})
            numbered_items.clear()

    for line in lines:
        item = re.match(r"^\(?\d+\)?[.)]?\s+(.+)$", line)
        if item:
            flush_paragraph()
            numbered_items.append(item.group(1).strip())
            continue
        flush_items()
        if re.match(r"^(SMCS Code|Model|Part|Serial|Rating|Capacity|Voltage|Frequency)\s*:", line, re.I):
            flush_paragraph()
            label, value = line.split(":", 1)
            sections.append({"type": "fact", "label": label.strip(), "value": value.strip()})
            continue
        words = re.findall(r"[A-Za-z][A-Za-z0-9/-]*", line)
        title_like = bool(words) and sum(word[:1].isupper() for word in words) >= max(1, len(words) - 1)
        if len(line) <= 72 and title_like and not re.search(r"[.!?]$", line):
            flush_paragraph()
            sections.append({"type": "heading", "text": line})
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
                if admin_manual and admin_manual["language"] == "en":
                    translation = conn.execute(
                        """
                        select mpt.final_translation, mpt.status
                        from manual_pages mp
                        join manual_page_translations mpt on mpt.manual_page_id = mp.id
                        where mp.manual_version_id = ? and mp.page_number = ? and mpt.language = 'ko'
                        """,
                        (admin_manual["manual_version_id"], selected_page),
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
    background_tasks.add_task(process_manual_document_safely, document_id)
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
