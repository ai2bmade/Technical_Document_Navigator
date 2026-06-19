from __future__ import annotations

import re

from app.retrieval import Hit, search
from app.storage import db


NO_EVIDENCE = "Document evidence not found."


def concise_answer(question: str, hits: list[Hit]) -> str:
    if not hits:
        return NO_EVIDENCE
    best = hits[0].content
    sentences = re.split(r"(?<=[.!?])\s+", best.replace("\n", " "))
    selected = " ".join(sentences[:4]).strip()
    if len(selected) > 900:
        selected = selected[:897].rstrip() + "..."
    evidence = "; ".join(
        f"{hit.filename} p.{hit.page_number}" for hit in hits[:3]
    )
    return f"{selected}\n\nEvidence: {evidence}"


def answer_question(question: str, document_id: int | None = None) -> dict[str, object]:
    hits = search(question, document_id=document_id, limit=5)
    answer = concise_answer(question, hits)
    needs_ocr = False
    if not hits and document_id is not None:
        with db() as conn:
            row = conn.execute(
                """
                select d.page_count, count(c.id) as chunk_count
                from documents d
                left join chunks c on c.document_id = d.id
                where d.id = ?
                group by d.id
                """,
                (document_id,),
            ).fetchone()
        needs_ocr = bool(row and row["page_count"] > 0 and row["chunk_count"] == 0)
        if needs_ocr:
            answer = "Document evidence not found. This PDF appears to require OCR before Q&A."
    with db() as conn:
        conn.execute(
            "insert into questions(question, answer) values (?, ?)",
            (question, answer),
        )
    return {
        "answer": answer,
        "evidence": [
            {
                "document_id": hit.document_id,
                "filename": hit.filename,
                "page_number": hit.page_number,
                "section_title": hit.section_title,
                "score": round(hit.score, 4),
                "excerpt": hit.content[:500],
            }
            for hit in hits
        ],
        "needs_ocr": needs_ocr,
    }


SPEC_FIELDS = {
    "equipment_model": ["model", "machine", "equipment"],
    "capacity": ["capacity", "ltr", "liter", "litre"],
    "dimensions": ["length", "width", "height", "dimension"],
    "power": ["power", "voltage", "kw", "hp"],
    "temperature": ["temperature", "heating", "cooling"],
    "manufacturer": ["manufacturer", "engineers", "company"],
    "installation_space": ["layout", "space", "clearance", "foundation"],
}


def analyze_spec(document_id: int) -> dict[str, object]:
    extracted: dict[str, list[dict[str, object]]] = {}
    for field, terms in SPEC_FIELDS.items():
        query = " ".join(terms)
        hits = search(query, document_id=document_id, limit=3)
        extracted[field] = [
            {
                "page_number": hit.page_number,
                "excerpt": hit.content[:700],
                "score": round(hit.score, 4),
            }
            for hit in hits
        ]
    return {"document_id": document_id, "fields": extracted}


def review_layout(document_id: int) -> dict[str, object]:
    checks = [
        ("installation_space", "Check required installation footprint and service clearance."),
        ("ceiling_height", "Confirm required ceiling height from layout notes."),
        ("utility_connections", "Identify power, air, fuel, exhaust, and ventilation needs."),
        ("missing_dimensions", "Flag any unclear or missing dimensions for engineer review."),
    ]
    return {
        "document_id": document_id,
        "notice": "Assistant review only. Final approval must remain with a qualified engineer.",
        "checks": [
            {
                "name": name,
                "instruction": instruction,
                "evidence": [
                    {
                        "page_number": hit.page_number,
                        "excerpt": hit.content[:500],
                        "score": round(hit.score, 4),
                    }
                    for hit in search(instruction, document_id=document_id, limit=2)
                ],
            }
            for name, instruction in checks
        ],
    }


def translation_workflow(text: str, target_language: str) -> dict[str, str]:
    return {
        "target_language": target_language,
        "step_1_initial_translation": "Pending LLM provider integration.",
        "step_2_technical_review": "Terminology memory review should run here.",
        "step_3_language_review": "Native-language fluency review should run here.",
        "step_4_final_consolidation": text,
    }
