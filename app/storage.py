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
  workspace text not null default 'manual',
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

create table if not exists analysis_reports (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  report_type text not null,
  report text not null,
  payload_json text,
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

create table if not exists manual_page_blocks (
  id integer primary key autoincrement,
  manual_version_id integer not null references manual_versions(id) on delete cascade,
  page_number integer not null,
  block_type text not null,
  reading_order integer not null,
  content text,
  asset_url text,
  caption text,
  metadata_json text,
  status text not null default 'draft',
  updated_at text not null default current_timestamp,
  unique(manual_version_id, page_number, reading_order)
);

create table if not exists document_page_corrections (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  page_number integer not null,
  raw_text text not null,
  corrected_text text not null,
  correction_notes text,
  uncertain_items text,
  confidence text,
  updated_at text not null default current_timestamp,
  unique(document_id, page_number)
);

create table if not exists manual_page_summaries (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  page_number integer not null,
  summary text,
  key_actions text,
  warnings text,
  confidence text,
  payload_json text,
  updated_at text not null default current_timestamp,
  unique(document_id, page_number)
);

create table if not exists manual_terms (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  term text not null,
  normalized_term text not null,
  definition text,
  aliases text,
  page_numbers text,
  confidence text,
  updated_at text not null default current_timestamp,
  unique(document_id, normalized_term)
);

create table if not exists manual_faqs (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  question text not null,
  answer text not null,
  related_terms text,
  evidence_pages text,
  confidence text,
  review_status text not null default 'ai_reviewed',
  updated_at text not null default current_timestamp
);

create table if not exists manual_qa_reviews (
  id integer primary key autoincrement,
  faq_id integer not null references manual_faqs(id) on delete cascade,
  reviewer_result text,
  issues text,
  revised_answer text,
  updated_at text not null default current_timestamp
);

create table if not exists manual_knowledge_runs (
  id integer primary key autoincrement,
  document_id integer not null references documents(id) on delete cascade,
  status text not null,
  pages_processed integer not null default 0,
  terms_count integer not null default 0,
  faqs_count integer not null default 0,
  message text,
  created_at text not null default current_timestamp
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
        columns = {
            row["name"]
            for row in conn.execute("pragma table_info(documents)").fetchall()
        }
        if "workspace" not in columns:
            conn.execute(
                "alter table documents add column workspace text not null default 'manual'"
            )
        conn.execute(
            """
            insert into manual_page_blocks(
              manual_version_id, page_number, block_type, reading_order, content, status
            )
            select
              mp.manual_version_id,
              mp.page_number,
              'paragraph',
              1,
              coalesce(mp.published_text, mp.ai_corrected_text, mp.raw_ocr_text, ''),
              case when mp.status like 'published%' then 'published' else 'draft' end
            from manual_pages mp
            where coalesce(mp.published_text, mp.ai_corrected_text, mp.raw_ocr_text, '') <> ''
              and not exists (
                select 1 from manual_page_blocks mb
                where mb.manual_version_id = mp.manual_version_id
                  and mb.page_number = mp.page_number
              )
            """
        )
