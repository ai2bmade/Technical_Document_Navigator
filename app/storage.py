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
