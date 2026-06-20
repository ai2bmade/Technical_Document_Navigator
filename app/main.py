from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import settings
from app.copilot import (
    analyze_spec,
    answer_question,
    page_action,
    review_layout,
    translation_workflow,
)
from app.manual_pipeline import (
    check_translation_accuracy,
    create_manual_version_from_document,
    generate_translation_draft,
    list_product_manuals,
    native_review_translation,
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
    with db() as conn:
        documents = conn.execute(
            """
            select d.id, d.filename, d.page_count, d.created_at, count(c.id) as chunk_count
            from documents d
            left join chunks c on c.document_id = d.id
            group by d.id
            order by d.id desc
            """
        ).fetchall()
        selected_document = None
        if documents:
            selected_id = document_id or documents[0]["id"]
            selected_document = conn.execute(
                """
                select d.id, d.filename, d.page_count, d.created_at, count(c.id) as chunk_count
                from documents d
                left join chunks c on c.document_id = d.id
                where d.id = ?
                group by d.id
                """,
                (selected_id,),
            ).fetchone()
        chunks = []
        page_chunks = []
        selected_page = 1
        previous_page = None
        next_page = None
        product_manuals = list_product_manuals()
        selected_manual = None
        selected_manual_page = None
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
def list_documents() -> list[dict[str, object]]:
    with db() as conn:
        rows = conn.execute(
            """
            select d.id, d.filename, d.page_count, d.created_at, count(c.id) as chunk_count
            from documents d
            left join chunks c on c.document_id = d.id
            group by d.id
            order by d.id desc
            """
        ).fetchall()
    documents = []
    for row in rows:
        item = dict(row)
        item["indexing_status"] = "needs_ocr" if item["chunk_count"] == 0 else "indexed"
        documents.append(item)
    return documents


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...)) -> dict[str, object]:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported in MVP.")
    with NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        temp.write(await file.read())
        temp_path = Path(temp.name)
    try:
        document_id = ingest_pdf(temp_path, display_name=file.filename)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
    return {"document_id": document_id, "filename": file.filename}


@app.post("/upload-form", response_class=HTMLResponse)
async def upload_form(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("manual_admin"),
) -> HTMLResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported in MVP.")
    with NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        temp.write(await file.read())
        temp_path = Path(temp.name)
    try:
        document_id = ingest_pdf(temp_path, display_name=file.filename)
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


@app.post("/product-manual-form", response_class=HTMLResponse)
def product_manual_form(
    request: Request,
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


@app.post("/api/documents/{document_id}/ocr")
def ocr_document(document_id: int, payload: OcrRequest | None = None) -> dict[str, object]:
    try:
        return run_ocr_for_document(
            document_id,
            replace_existing=payload.replace_existing if payload else False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500, detail=exc.stderr or str(exc)) from exc


@app.post("/api/product-manuals/from-document")
def product_manual_from_document(payload: ProductManualRequest) -> dict[str, object]:
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
    return {
        "manual_version_id": manual_version_id,
        "product_slug": payload.product_slug,
        "language": payload.language,
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
    return analyze_spec(payload.document_id)


@app.post("/api/layout/review")
def layout_review(payload: AnalyzeRequest) -> dict[str, object]:
    return review_layout(payload.document_id)


@app.post("/spec-review-form", response_class=HTMLResponse)
def spec_review_form(
    request: Request,
    document_id: int = Form(...),
    page: int = Form(1),
) -> HTMLResponse:
    result = analyze_spec(document_id)
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
    result = review_layout(document_id)
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
    result = answer_question(question, document_id)
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
def ocr_form(request: Request, document_id: int = Form(...)) -> HTMLResponse:
    result = run_ocr_for_document(document_id)
    workspace = load_workspace(document_id, mode="manual_admin")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "ocr_result": result,
            **workspace,
        },
    )
