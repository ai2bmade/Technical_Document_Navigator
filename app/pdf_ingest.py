from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from app.config import settings
from app.storage import db


@dataclass(frozen=True)
class Chunk:
    page_number: int
    section_title: str | None
    content: str


def clean_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def split_chunks(text: str, page_number: int, max_chars: int = 1400) -> list[Chunk]:
    text = clean_text(text)
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[Chunk] = []
    buffer = ""
    section_title: str | None = None
    for paragraph in paragraphs:
        if len(paragraph) < 80 and paragraph.endswith(":"):
            section_title = paragraph.rstrip(":")
        if len(buffer) + len(paragraph) + 2 > max_chars and buffer:
            chunks.append(Chunk(page_number, section_title, buffer.strip()))
            buffer = ""
        buffer = f"{buffer}\n\n{paragraph}".strip()
    if buffer:
        chunks.append(Chunk(page_number, section_title, buffer.strip()))
    return chunks


def ingest_pdf(source_path: Path, display_name: str | None = None) -> int:
    reader = PdfReader(str(source_path))
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    original_name = display_name or source_path.name
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", original_name)
    stored_path = settings.uploads_dir / safe_name
    if source_path.resolve() != stored_path.resolve():
        shutil.copy2(source_path, stored_path)

    chunks: list[Chunk] = []
    for index, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""
        chunks.extend(split_chunks(page_text, index))

    with db() as conn:
        cursor = conn.execute(
            "insert into documents(filename, stored_path, page_count) values (?, ?, ?)",
            (original_name, str(stored_path), len(reader.pages)),
        )
        document_id = int(cursor.lastrowid)
        conn.executemany(
            """
            insert into chunks(document_id, page_number, section_title, content)
            values (?, ?, ?, ?)
            """,
            [
                (document_id, chunk.page_number, chunk.section_title, chunk.content)
                for chunk in chunks
            ],
        )
    return document_id
