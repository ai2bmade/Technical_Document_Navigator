# Quick Deployment

This MVP is ready to deploy as two runtime processes:

- `app`: FastAPI web application
- `telegram-bot`: Telegram long polling worker

Both services share the same `/data` volume so uploaded PDFs, OCR output chunks,
and the SQLite database are visible to both web and Telegram.

## Coolify

1. Create a new project in Coolify.
2. Add a new resource from GitHub:
   `https://github.com/ai2bmade/Technical_Document_Navigator`
3. Select Docker Compose deployment.
4. Use `docker-compose.yml` from the repository root.
5. Set the public domain on the `app` service.
6. Do not expose a domain or port for `telegram-bot`.
7. Add environment variables.
8. Deploy.

## Required Environment Variables

```text
APP_DATA_DIR=/data
TELEGRAM_BOT_TOKEN=your_botfather_token
TESSERACT_CMD=tesseract
OCR_LANG=kor+eng
OCR_DPI=220
```

Optional:

```text
OPENAI_API_KEY=
DATABASE_URL=postgresql://industrial:industrial@postgres:5432/industrial_copilot
CHROMA_HOST=chroma
CHROMA_PORT=8000
```

## First Demo Flow

1. Open the web URL.
2. Upload `CS-20000-OPS-and-Layout.pdf`.
3. Ask: `What is the CS-20000 capacity?`
4. Upload `GX25_GX35.pdf`.
5. Click `Run OCR`.
6. Ask: `How do I check the engine oil level?`
7. Open Telegram and send `/help`.
8. Send a document question in Telegram.

## Current MVP Limitations

- Telegram currently answers against all indexed documents.
- Telegram PDF upload is not implemented yet.
- Web upload and OCR are the main document ingestion path.
- Answer quality is lexical retrieval plus evidence excerpts, not full LLM
  reasoning yet.
