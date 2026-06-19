# Industrial Technical Document Copilot MVP

Runnable MVP scaffold for an industrial technical-document assistant.

It supports:

- PDF upload and parsing
- Page/chunk search with evidence
- Manual Q&A over uploaded documents
- Specification extraction
- Translation workflow scaffold
- Layout-review checklist scaffold
- Web UI
- Telegram bot command entry points
- Docker/Coolify deployment files

The first implementation favors accuracy and evidence over speed. If the answer
cannot be grounded in extracted text, the API returns: `Document evidence not found.`

## Quick Start

```powershell
cd outputs\industrial-copilot-mvp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

## Upload The Sample PDFs

The app does not require bundled sample files. For this MVP, the reference
documents used during validation were:

- Manual sample: Honda GX25/GX35 Korean manual, `GX25_GX35.pdf`
- Specification/layout sample: `CS-20000-OPS-and-Layout.pdf`

Use the web form or API:

```powershell
curl.exe -F "file=@F:\다운로드\GX25_GX35.pdf" http://localhost:8000/api/documents
curl.exe -F "file=@F:\다운로드\CS-20000-OPS-and-Layout.pdf" http://localhost:8000/api/documents
```

## API Examples

```powershell
curl.exe -X POST http://localhost:8000/api/ask `
  -H "Content-Type: application/json" `
  -d "{\"question\":\"When should the oil be changed?\"}"
```

```powershell
curl.exe -X POST http://localhost:8000/api/spec/analyze `
  -H "Content-Type: application/json" `
  -d "{\"document_id\":1}"
```

## Environment

Copy `.env.example` to `.env` for local overrides.

Important settings:

- `APP_DATA_DIR`: persistent database, uploads, vector chunks, logs
- `DATABASE_URL`: optional PostgreSQL URL for future migration
- `TELEGRAM_BOT_TOKEN`: enables the Telegram bot
- `OPENAI_API_KEY`: optional future LLM provider key
- `TESSERACT_CMD`: OCR executable path
- `OCR_LANG`: OCR languages, defaults to `kor+eng`
- `OCR_DPI`: PDF render DPI for OCR, defaults to `220`

Local Windows OCR path used for this MVP:

```text
G:\Codex\tools\tesseract\Tesseract-OCR\tesseract.exe
```

## Deployment

`docker-compose.yml` includes:

- FastAPI app
- PostgreSQL
- Chroma

For Coolify, create a Docker Compose service from this directory and set a
public domain such as `https://joshuajhchoi.cloud`.

The Docker image installs `tesseract-ocr` and `tesseract-ocr-kor`, so Korean and
English OCR work inside the container with `TESSERACT_CMD=tesseract`.

## Notes

This MVP uses SQLite plus a local lexical retriever so it can run immediately.
The repository is structured so PostgreSQL, Chroma, OCR, and provider LLMs can
be enabled incrementally without changing the user-facing API.
