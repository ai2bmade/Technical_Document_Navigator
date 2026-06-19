# MVP Sample Documents

These files are validation samples only. The application must also work with no
sample documents uploaded.

## Manual Sample

- File: `F:\다운로드\GX25_GX35.pdf`
- Document type: Honda GX25/GX35 Korean engine manual
- MVP module: Manual Learning Assistant
- Current status: image/scanned PDF in the local validation run, so OCR is
  required before reliable Q&A.

## Specification And Layout Sample

- File: `F:\다운로드\CS-20000-OPS-and-Layout.pdf`
- Document type: CS-20000 specification sheet and layout drawing
- MVP modules: Technical Specification Analyzer, Layout Drawing Review Assistant
- Current status: text extraction works, page-level evidence Q&A was validated.

## Design Rule

The repository should not depend on these local files being present. Users can
upload any supported PDF through the web UI or `/api/documents`.
