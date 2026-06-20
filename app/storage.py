from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings


SCHEMA = """
create table if not exists documents (
  id integer primary key autoincrement,
  filename text not null,
  stored_path text not null,
  page_count integer not null,
  created_at text not null default current_timestamp
);

create table if not exists chunks (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  page_number integer not null,
  section_title text,
  content text not null
);

create table if not exists questions (
  id integer primary key autoincrement,
  question text not null,
  answer text not null,
  created_at text not null default current_timestamp
);

create table if not exists product_families (
  id integer primary key autoincrement,
  slug text not null unique,
  display_name text not null,
  manufacturer text,
  model_group text,
  default_language text not null default 'ko',
  status text not null default 'draft',
  created_at text not null default current_timestamp
);

create table if not exists manual_versions (
  id integer primary key autoincrement,
  product_family_id integer not null references product_families(id) on delete cascade,
  source_document_id integer references documents(id) on delete set null,
  language text not null,
  title text not null,
  status text not null default 'draft',
  created_at text not null default current_timestamp,
  unique(product_family_id, language)
);

create table if not exists manual_pages (
  id integer primary key autoincrement,
  manual_version_id integer not null references manual_versions(id) on delete cascade,
  page_number integer not null,
  raw_ocr_text text,
  ai_corrected_text text,
  published_text text,
  summary text,
  warnings text,
  status text not null default 'raw',
  updated_at text not null default current_timestamp,
  unique(manual_version_id, page_number)
);

create table if not exists manual_page_translations (
  id integer primary key autoincrement,
  manual_page_id integer not null references manual_pages(id) on delete cascade,
  language text not null,
  draft_translation text,
  accuracy_checked_translation text,
  final_translation text,
  accuracy_issues text,
  native_review_notes text,
  status text not null default 'draft',
  updated_at text not null default current_timestamp,
  unique(manual_page_id, language)
);
"""


def ensure_dirs() -> None:
    for path in [
        settings.app_data_dir,
        settings.uploads_dir,
        settings.logs_dir,
        settings.vector_dir,
        settings.sqlite_path.parent,
    ]:
        Path(path).mkdir(parents=True, exist_ok=True)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
