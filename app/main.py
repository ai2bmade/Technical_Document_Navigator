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
from app.copilot import analyze_spec, answer_question, review_layout, translation_workflow
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


def load_workspace(document_id: int | None = None) -> dict[str, object]:
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
        if selected_document:
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
    return {
        "documents": documents,
        "selected_document": selected_document,
        "chunks": chunks,
    }


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, document_id: int | None = None) -> HTMLResponse:
    workspace = load_workspace(document_id)
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
async def upload_form(request: Request, file: UploadFile = File(...)) -> HTMLResponse:
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
    workspace = load_workspace(document_id)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "upload_message": f"Uploaded {file.filename}",
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
) -> HTMLResponse:
    result = answer_question(question, document_id)
    workspace = load_workspace(document_id)
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


@app.post("/ocr-form", response_class=HTMLResponse)
def ocr_form(request: Request, document_id: int = Form(...)) -> HTMLResponse:
    result = run_ocr_for_document(document_id)
    workspace = load_workspace(document_id)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "ocr_result": result,
            **workspace,
        },
    )
