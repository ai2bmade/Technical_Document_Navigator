from __future__ import annotations

from pathlib import Path

import fitz

from app.ocr import document_path


def render_page_png(document_id: int, page_number: int, dpi: int = 150) -> bytes:
    if page_number < 1:
        raise ValueError("Page number must be 1 or greater.")

    pdf_path: Path = document_path(document_id)
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(pdf_path) as document:
        if page_number > len(document):
            raise ValueError(f"Page {page_number} is outside this document.")
        page = document[page_number - 1]
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        return pixmap.tobytes("png")
