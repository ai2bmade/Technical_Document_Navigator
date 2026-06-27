from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import fitz

from app.config import settings
from app.pdf_ingest import split_chunks
from app.storage import db


@dataclass(frozen=True)
class OcrPage:
    page_number: int
    text: str


def document_path(document_id: int) -> Path:
    with db() as conn:
        row = conn.execute(
            "select stored_path from documents where id = ?",
            (document_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Document {document_id} was not found.")
    path = Path(row["stored_path"])
    if not path.exists():
        raise FileNotFoundError(f"Stored PDF was not found: {path}")
    return path


def run_tesseract(image_path: Path, psm: int = 6) -> str:
    command = [
        settings.tesseract_cmd,
        str(image_path),
        "stdout",
        "-l",
        settings.ocr_lang,
        "--psm",
        str(psm),
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def ocr_page_region(
    document_id: int,
    page_number: int,
    x_percent: float,
    y_percent: float,
    width_percent: float,
    height_percent: float,
    dpi: int = 400,
) -> str:
    if page_number < 1:
        raise ValueError("Page number must be 1 or greater.")
    values = (x_percent, y_percent, width_percent, height_percent)
    if not all(0 <= value <= 100 for value in values[:2]):
        raise ValueError("Invalid region position.")
    if not (0.5 <= width_percent <= 100 and 0.5 <= height_percent <= 100):
        raise ValueError("The selected region is too small or invalid.")

    pdf_path = document_path(document_id)
    settings.ocr_tmp_dir.mkdir(parents=True, exist_ok=True)
    image_path = settings.ocr_tmp_dir / f"region_{document_id}_{uuid4().hex}.png"
    with fitz.open(pdf_path) as document:
        if page_number > len(document):
            raise ValueError("Page number is outside the document.")
        page = document[page_number - 1]
        page_rect = page.rect
        clip = fitz.Rect(
            page_rect.x0 + page_rect.width * x_percent / 100,
            page_rect.y0 + page_rect.height * y_percent / 100,
            page_rect.x0 + page_rect.width * (x_percent + width_percent) / 100,
            page_rect.y0 + page_rect.height * (y_percent + height_percent) / 100,
        ) & page_rect
        if clip.is_empty:
            raise ValueError("The selected region is outside the page.")
        requested_scale = dpi / 72
        max_scale = math.sqrt(40_000_000 / max(1, clip.width * clip.height))
        scale = min(requested_scale, max_scale)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        pixmap.save(image_path)
    try:
        return run_tesseract(image_path, psm=11)
    finally:
        image_path.unlink(missing_ok=True)


def ocr_pdf_pages(pdf_path: Path) -> list[OcrPage]:
    settings.ocr_tmp_dir.mkdir(parents=True, exist_ok=True)
    pages: list[OcrPage] = []
    zoom = settings.ocr_dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(pdf_path) as document:
        for index, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            image_path = settings.ocr_tmp_dir / f"{pdf_path.stem}_page_{index:04d}.png"
            pixmap.save(image_path)
            try:
                text = run_tesseract(image_path)
            finally:
                image_path.unlink(missing_ok=True)
            pages.append(OcrPage(page_number=index, text=text))
    return pages


def run_ocr_for_document(document_id: int, replace_existing: bool = False) -> dict[str, object]:
    path = document_path(document_id)
    with db() as conn:
        chunk_count = conn.execute(
            "select count(*) from chunks where document_id = ?",
            (document_id,),
        ).fetchone()[0]
    if chunk_count and not replace_existing:
        return {
            "document_id": document_id,
            "status": "skipped",
            "message": "Document already has extracted text chunks.",
            "chunks_added": 0,
        }

    pages = ocr_pdf_pages(path)
    rows: list[tuple[int, int, str | None, str]] = []
    for page in pages:
        for chunk in split_chunks(page.text, page.page_number):
            rows.append((document_id, chunk.page_number, chunk.section_title, chunk.content))

    with db() as conn:
        if replace_existing:
            conn.execute("delete from chunks where document_id = ?", (document_id,))
        conn.executemany(
            """
            insert into chunks(document_id, page_number, section_title, content)
            values (?, ?, ?, ?)
            """,
            rows,
        )

    return {
        "document_id": document_id,
        "status": "ocr_completed",
        "pages_processed": len(pages),
        "chunks_added": len(rows),
        "language": settings.ocr_lang,
        "dpi": settings.ocr_dpi,
    }
