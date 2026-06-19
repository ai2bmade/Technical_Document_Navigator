from __future__ import annotations

import argparse
from pathlib import Path

from app.pdf_ingest import ingest_pdf
from app.storage import init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Import sample PDFs.")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    init_db()
    for path in args.paths:
        document_id = ingest_pdf(path)
        print(f"Imported {path} as document {document_id}")


if __name__ == "__main__":
    main()
