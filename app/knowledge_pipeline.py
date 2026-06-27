from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from app.openai_service import generate_text
from app.pdf_ingest import split_chunks
from app.storage import db


def page_texts(document_id: int) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            select page_number, group_concat(content, char(10) || char(10)) as content
            from chunks
            where document_id = ?
            group by page_number
            order by page_number
            """,
            (document_id,),
        ).fetchall()
    return [
        {"page_number": int(row["page_number"]), "content": row["content"] or ""}
        for row in rows
        if (row["content"] or "").strip()
    ]


def document_knowledge_context(document_id: int, limit: int = 9000) -> str:
    with db() as conn:
        try:
            summaries = conn.execute(
                """
                select page_number, summary, key_actions, warnings
                from manual_page_summaries
                where document_id = ?
                order by page_number
                """,
                (document_id,),
            ).fetchall()
            terms = conn.execute(
                """
                select term, definition, aliases, page_numbers
                from manual_terms
                where document_id = ?
                order by term
                limit 80
                """,
                (document_id,),
            ).fetchall()
            faqs = conn.execute(
                """
                select question, answer, evidence_pages
                from manual_faqs
                where document_id = ?
                order by id
                limit 80
                """,
                (document_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return ""

    parts: list[str] = []
    if summaries:
        parts.append("PAGE SUMMARIES")
        for row in summaries:
            parts.append(
                f"[p.{row['page_number']}]\n"
                f"Summary: {row['summary'] or ''}\n"
                f"Actions: {row['key_actions'] or ''}\n"
                f"Warnings: {row['warnings'] or ''}"
            )
    if terms:
        parts.append("TERMS")
        for row in terms:
            parts.append(
                f"- {row['term']}: {row['definition'] or ''} "
                f"(aliases: {row['aliases'] or '-'}, pages: {row['page_numbers'] or '-'})"
            )
    if faqs:
        parts.append("FAQ")
        for row in faqs:
            parts.append(
                f"Q: {row['question']}\n"
                f"A: {row['answer']}\n"
                f"Pages: {row['evidence_pages'] or '-'}"
            )
    text = "\n\n".join(parts)
    return text[:limit]


def faq_candidates_for_question(document_id: int, question: str, limit: int = 8) -> list[dict[str, Any]]:
    tokens = {
        token
        for token in re.findall(r"[A-Za-z0-9가-힣+-]{2,}", question.lower())
        if token not in {"what", "when", "where", "how", "the", "and", "for", "with"}
    }
    with db() as conn:
        try:
            rows = conn.execute(
                """
                select question, answer, related_terms, evidence_pages, confidence
                from manual_faqs
                where document_id = ?
                order by id
                """,
                (document_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        haystack = " ".join(
            [
                row["question"] or "",
                row["answer"] or "",
                row["related_terms"] or "",
            ]
        ).lower()
        value = sum(1 for token in tokens if token in haystack)
        if value:
            scored.append((value, dict(row)))
    return [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]


def _loads_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def correct_document_pages(document_id: int) -> dict[str, Any]:
    pages = page_texts(document_id)
    corrected_pages = 0
    uncertain_count = 0
    errors: list[str] = []
    for page in pages:
        raw_text = page["content"].strip()
        if not raw_text:
            continue
        try:
            response = generate_text(
                (
                    "You are a meticulous technical-document OCR correction editor. "
                    "Keep the source language unchanged. Correct only highly probable OCR errors using context. "
                    "Preserve model numbers, part numbers, measurements, units, warning labels, and procedure order. "
                    "Never guess an ambiguous number or technical identifier; keep it and mark [CHECK]. "
                    "Do not summarize, translate, or rewrite the author's meaning. Return valid JSON only."
                ),
                (
                    f"Document ID: {document_id}\nPage: {page['page_number']}\n\n"
                    "JSON schema:\n"
                    "{\n"
                    '  "corrected_text": "faithful corrected text",\n'
                    '  "correction_notes": ["important corrections"],\n'
                    '  "uncertain_items": ["items requiring human verification"],\n'
                    '  "confidence": "high|medium|low"\n'
                    "}\n\n"
                    f"Raw extracted text:\n{raw_text[:12000]}"
                ),
            )
            payload = _loads_json_object(response)
            corrected_text = str(payload.get("corrected_text") or raw_text).strip()
            uncertain_items = payload.get("uncertain_items") or []
            with db() as conn:
                conn.execute(
                    """
                    insert into document_page_corrections(
                      document_id, page_number, raw_text, corrected_text,
                      correction_notes, uncertain_items, confidence
                    )
                    values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(document_id, page_number) do update set
                      corrected_text = excluded.corrected_text,
                      correction_notes = excluded.correction_notes,
                      uncertain_items = excluded.uncertain_items,
                      confidence = excluded.confidence,
                      updated_at = current_timestamp
                    """,
                    (
                        document_id,
                        page["page_number"],
                        raw_text,
                        corrected_text,
                        json.dumps(payload.get("correction_notes") or [], ensure_ascii=False),
                        json.dumps(uncertain_items, ensure_ascii=False),
                        payload.get("confidence") or "medium",
                    ),
                )
                conn.execute(
                    "delete from chunks where document_id = ? and page_number = ?",
                    (document_id, page["page_number"]),
                )
                rows = [
                    (document_id, chunk.page_number, chunk.section_title, chunk.content)
                    for chunk in split_chunks(corrected_text, int(page["page_number"]))
                ]
                conn.executemany(
                    """
                    insert into chunks(document_id, page_number, section_title, content)
                    values (?, ?, ?, ?)
                    """,
                    rows,
                )
            corrected_pages += 1
            uncertain_count += len(uncertain_items)
        except Exception as exc:
            errors.append(f"p.{page['page_number']}: {exc}")
    return {
        "document_id": document_id,
        "status": "completed" if not errors else "partial",
        "corrected_pages": corrected_pages,
        "uncertain_items": uncertain_count,
        "errors": errors[:5],
    }


def analyze_manual_page(document_id: int, page_number: int, text: str) -> dict[str, Any]:
    instructions = (
        "You convert noisy OCR from an industrial/product manual into structured customer-support knowledge. "
        "Use Korean for summaries and definitions unless source terms must remain as-is. "
        "Do not invent facts. Preserve numbers, warning labels, model names, button names, and procedure order. "
        "Return valid JSON only."
    )
    prompt = (
        f"Document ID: {document_id}\n"
        f"Page: {page_number}\n\n"
        "Extract structured knowledge from this OCR text.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "summary": "page meaning in Korean",\n'
        '  "key_actions": ["customer actions or procedures"],\n'
        '  "warnings": ["warnings, cautions, safety notes"],\n'
        '  "terms": [\n'
        '    {"term": "visible term", "normalized_term": "canonical key", "definition": "meaning", "aliases": ["alias"], "confidence": "high|medium|low"}\n'
        "  ],\n"
        '  "faqs": [\n'
        '    {"question": "likely customer question", "answer": "grounded answer", "related_terms": ["term"], "evidence_pages": [page_number], "confidence": "high|medium|low"}\n'
        "  ],\n"
        '  "qa_reviews": [\n'
        '    {"question": "faq question", "reviewer_result": "pass|needs_check", "issues": "issue or empty", "revised_answer": "corrected answer"}\n'
        "  ],\n"
        '  "confidence": "high|medium|low"\n'
        "}\n\n"
        f"OCR text:\n{text[:7000]}"
    )
    raw = generate_text(instructions, prompt)
    payload = _loads_json_object(raw)
    payload["_raw_model_output"] = raw
    return payload


def _json_list(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return json.dumps([value], ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def save_page_knowledge(document_id: int, page_number: int, payload: dict[str, Any]) -> tuple[int, int]:
    terms = payload.get("terms") or []
    faqs = payload.get("faqs") or []
    key_actions = payload.get("key_actions") or []
    warnings = payload.get("warnings") or []
    with db() as conn:
        conn.execute(
            """
            insert into manual_page_summaries(
              document_id, page_number, summary, key_actions, warnings, confidence, payload_json
            )
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(document_id, page_number) do update set
              summary = excluded.summary,
              key_actions = excluded.key_actions,
              warnings = excluded.warnings,
              confidence = excluded.confidence,
              payload_json = excluded.payload_json,
              updated_at = current_timestamp
            """,
            (
                document_id,
                page_number,
                payload.get("summary") or "",
                _json_list(key_actions),
                _json_list(warnings),
                payload.get("confidence") or "medium",
                json.dumps(payload, ensure_ascii=False),
            ),
        )

        for item in terms:
            term = str(item.get("term") or "").strip()
            normalized = str(item.get("normalized_term") or term).strip().lower()
            if not term or not normalized:
                continue
            existing = conn.execute(
                """
                select page_numbers from manual_terms
                where document_id = ? and normalized_term = ?
                """,
                (document_id, normalized),
            ).fetchone()
            pages = {page_number}
            if existing and existing["page_numbers"]:
                try:
                    pages.update(int(page) for page in json.loads(existing["page_numbers"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            conn.execute(
                """
                insert into manual_terms(
                  document_id, term, normalized_term, definition, aliases, page_numbers, confidence
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(document_id, normalized_term) do update set
                  term = excluded.term,
                  definition = coalesce(nullif(excluded.definition, ''), manual_terms.definition),
                  aliases = excluded.aliases,
                  page_numbers = excluded.page_numbers,
                  confidence = excluded.confidence,
                  updated_at = current_timestamp
                """,
                (
                    document_id,
                    term,
                    normalized,
                    item.get("definition") or "",
                    _json_list(item.get("aliases") or []),
                    json.dumps(sorted(pages), ensure_ascii=False),
                    item.get("confidence") or "medium",
                ),
            )

        for item in faqs:
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if not question or not answer:
                continue
            cur = conn.execute(
                """
                insert into manual_faqs(
                  document_id, question, answer, related_terms, evidence_pages, confidence, review_status
                )
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    question,
                    answer,
                    _json_list(item.get("related_terms") or []),
                    _json_list(item.get("evidence_pages") or [page_number]),
                    item.get("confidence") or "medium",
                    "ai_reviewed",
                ),
            )
            faq_id = int(cur.lastrowid)
            review = next(
                (
                    review
                    for review in (payload.get("qa_reviews") or [])
                    if str(review.get("question") or "").strip() == question
                ),
                None,
            )
            if review:
                conn.execute(
                    """
                    insert into manual_qa_reviews(faq_id, reviewer_result, issues, revised_answer)
                    values (?, ?, ?, ?)
                    """,
                    (
                        faq_id,
                        review.get("reviewer_result") or "",
                        review.get("issues") or "",
                        review.get("revised_answer") or answer,
                    ),
                )
    return len(terms), len(faqs)


def build_manual_knowledge(document_id: int, replace_existing: bool = True) -> dict[str, Any]:
    pages = page_texts(document_id)
    if not pages:
        return {
            "document_id": document_id,
            "status": "skipped",
            "message": "No OCR chunks were found.",
            "pages_processed": 0,
            "terms_count": 0,
            "faqs_count": 0,
        }

    if replace_existing:
        with db() as conn:
            conn.execute("delete from manual_page_summaries where document_id = ?", (document_id,))
            conn.execute("delete from manual_terms where document_id = ?", (document_id,))
            conn.execute("delete from manual_faqs where document_id = ?", (document_id,))

    terms_count = 0
    faqs_count = 0
    pages_processed = 0
    errors: list[str] = []
    for page in pages:
        try:
            payload = analyze_manual_page(document_id, page["page_number"], page["content"])
            terms_added, faqs_added = save_page_knowledge(
                document_id,
                page["page_number"],
                payload,
            )
            terms_count += terms_added
            faqs_count += faqs_added
            pages_processed += 1
        except Exception as exc:  # Keep later pages useful even if one model output is malformed.
            errors.append(f"p.{page['page_number']}: {exc}")

    status = "completed" if not errors else "partial"
    message = "; ".join(errors[:5])
    with db() as conn:
        conn.execute(
            """
            insert into manual_knowledge_runs(
              document_id, status, pages_processed, terms_count, faqs_count, message
            )
            values (?, ?, ?, ?, ?, ?)
            """,
            (document_id, status, pages_processed, terms_count, faqs_count, message),
        )
    return {
        "document_id": document_id,
        "status": status,
        "pages_processed": pages_processed,
        "terms_count": terms_count,
        "faqs_count": faqs_count,
        "message": message,
    }
