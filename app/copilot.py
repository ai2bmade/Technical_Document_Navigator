from __future__ import annotations

import re

from app.openai_service import generate_text
from app.retrieval import Hit, search
from app.storage import db


NO_EVIDENCE = "Document evidence not found."


ANSWER_LANGUAGE_NAMES = {
    "ko": "Korean",
    "en": "English",
    "es": "Spanish",
    "ar": "Arabic",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
}


def compact_text(text: str, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def page_action(action: str, page_text: str, filename: str, page_number: int) -> dict[str, object]:
    text = compact_text(page_text, 1600)
    if not text:
        answer = "이 페이지에서 확인 가능한 OCR 텍스트가 없습니다. 원본 이미지를 보고 직접 확인하거나 OCR을 다시 실행해 주세요."
    elif action == "summary":
        answer = (
            "현재 페이지 요약\n\n"
            f"{compact_text(text, 700)}\n\n"
            f"근거: {filename} p.{page_number}"
        )
    elif action == "easy":
        answer = (
            "쉽게 설명\n\n"
            "이 페이지는 아래 내용을 중심으로 읽으면 됩니다.\n\n"
            f"{compact_text(text, 700)}\n\n"
            f"근거: {filename} p.{page_number}"
        )
    elif action == "warnings":
        lines = [
            line.strip()
            for line in page_text.splitlines()
            if any(word in line for word in ["주의", "경고", "위험", "안전", "금지"])
        ]
        if lines:
            answer = "주의사항\n\n" + "\n".join(f"- {compact_text(line, 180)}" for line in lines[:8])
        else:
            answer = "이 페이지 OCR 텍스트에서는 명확한 주의/경고 문구를 찾지 못했습니다."
        answer += f"\n\n근거: {filename} p.{page_number}"
    elif action == "specs":
        lines = [
            line.strip()
            for line in page_text.splitlines()
            if re.search(r"\b(mm|cm|m|kg|kw|hp|v|hz|rpm|ltr|l)\b|[0-9]", line.lower())
        ]
        if lines:
            answer = "관련 수치/사양 후보\n\n" + "\n".join(f"- {compact_text(line, 180)}" for line in lines[:10])
        else:
            answer = "이 페이지 OCR 텍스트에서는 수치/사양 후보를 찾지 못했습니다."
        answer += f"\n\n근거: {filename} p.{page_number}"
    else:
        answer = "지원하지 않는 빠른 작업입니다."

    return {
        "answer": answer,
        "evidence": [
            {
                "filename": filename,
                "page_number": page_number,
                "excerpt": compact_text(page_text, 500),
            }
        ],
    }


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


def answer_question_with_ai(
    question: str,
    document_id: int | None = None,
    language: str = "ko",
) -> dict[str, object]:
    hits = search(question, document_id=document_id, limit=6)
    language_name = ANSWER_LANGUAGE_NAMES.get(language.lower(), language)
    context = document_context(document_id, max_chars=14000) if document_id else ""
    if not hits:
        if context:
            answer = generate_text(
                (
                    "You are a careful customer-support assistant for an industrial product manual. "
                    f"Answer in {language_name}. Use only the selected manual context provided below. "
                    "If the context does not support the answer, say that the manual evidence is insufficient. "
                    "Do not invent specifications, safety instructions, or procedures. Preserve numbers, units, "
                    "model names, warning words, and button labels exactly when they appear in the evidence."
                ),
                (
                    "Search did not find a focused passage, so use this broader selected-manual context.\n\n"
                    f"Customer question:\n{question}\n\n"
                    f"Manual context:\n{context}\n\n"
                    "Return a concise customer-facing answer with page references when visible in the context."
                ),
            )
            return {
                "answer": answer,
                "evidence": [],
                "needs_ocr": False,
                "engine": "openai_broad_context",
            }
        answer = generate_text(
            (
                "You are a careful customer-support assistant for an industrial product manual. "
                f"Answer in {language_name}. If the question is not supported by the selected manual, "
                "say that the manual evidence was not found. Do not invent specifications, safety instructions, "
                "or procedures. You may briefly suggest what manual section or support channel to check next."
            ),
            (
                "No matching manual evidence was found for the selected document.\n\n"
                f"Customer question:\n{question}"
            ),
        )
        return {"answer": answer, "evidence": [], "needs_ocr": False, "engine": "openai"}

    evidence_blocks = []
    for index, hit in enumerate(hits, start=1):
        evidence_blocks.append(
            f"[Evidence {index}: {hit.filename} p.{hit.page_number}]\n{compact_text(hit.content, 1200)}"
        )
    instructions = (
        "You are a high-quality customer-support assistant for an industrial product manual. "
        f"Answer in {language_name}. Use the selected manual context as the primary source, and use search hits only as hints. "
        "First understand the customer's intent semantically. Do not blindly repeat the top search hit if it does not answer the question. "
        "If the manual context is incomplete or the customer asks something unrelated, say so clearly and do not guess. "
        "Preserve model names, numbers, units, warning words, and button labels exactly when they appear in the evidence. "
        "Give a practical answer customers can use. Include page references only when the page marker or search hit supports them."
    )
    prompt = (
        f"Customer question:\n{question}\n\n"
        "Selected manual broader context:\n"
        f"{context}\n\n"
        "Focused search candidates, which may be imperfect:\n"
        + "\n\n".join(evidence_blocks)
        + "\n\n"
        "Important: if the focused search candidates are about a different feature than the customer's question, ignore those candidates and use the broader context.\n\n"
        "Return format:\n"
        "1. Direct answer\n"
        "2. Important caution or limitation, if any\n"
        "3. Evidence pages"
    )
    answer = generate_text(instructions, prompt)
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
        "needs_ocr": False,
        "engine": "openai",
    }


def translate_customer_text(text: str, language: str) -> str:
    if not text or language.lower() == "ko":
        return text
    language_name = ANSWER_LANGUAGE_NAMES.get(language.lower(), language)
    return generate_text(
        (
            "You are a professional industrial manual translator. "
            f"Translate into {language_name}. Preserve numbers, units, model names, warnings, button labels, "
            "and procedure order. Do not add content. If OCR text is unclear, keep the uncertainty visible."
        ),
        f"Translate this customer manual text:\n\n{text}",
    )


SPEC_FIELDS = {
    "equipment_model": ["model", "machine", "equipment"],
    "capacity": ["capacity", "ltr", "liter", "litre"],
    "dimensions": ["length", "width", "height", "dimension"],
    "power": ["power", "voltage", "kw", "hp"],
    "temperature": ["temperature", "heating", "cooling"],
    "manufacturer": ["manufacturer", "engineers", "company"],
    "installation_space": ["layout", "space", "clearance", "foundation"],
}


def document_context(document_id: int, max_chars: int = 18000) -> str:
    with db() as conn:
        rows = conn.execute(
            """
            select page_number, content
            from chunks
            where document_id = ?
            order by page_number, id
            """,
            (document_id,),
        ).fetchall()
    parts: list[str] = []
    total = 0
    for row in rows:
        content = row["content"].strip()
        if not content:
            continue
        block = f"[p.{row['page_number']}]\n{content}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def document_filename(document_id: int) -> str:
    with db() as conn:
        row = conn.execute(
            "select filename from documents where id = ?",
            (document_id,),
        ).fetchone()
    return row["filename"] if row else f"document #{document_id}"


def evidence_candidates(document_id: int, queries: list[str], limit: int = 3) -> list[dict[str, object]]:
    seen: set[tuple[int, str]] = set()
    evidence: list[dict[str, object]] = []
    for query in queries:
        for hit in search(query, document_id=document_id, limit=limit):
            key = (hit.page_number, hit.content[:120])
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                {
                    "page_number": hit.page_number,
                    "excerpt": compact_text(hit.content, 500),
                    "score": round(hit.score, 4),
                }
            )
    return evidence[:12]


def ensure_korean_first_report(report: str, report_type: str) -> str:
    hangul_count = len(re.findall(r"[가-힣]", report))
    latin_count = len(re.findall(r"[A-Za-z]", report))
    if hangul_count >= 80 or hangul_count >= latin_count:
        return report
    instructions = (
        "당신은 산업 기술 문서를 한국어 리포트로 정리하는 편집자입니다. "
        "아래 리포트를 한국어를 최우선으로 하는 자연스러운 기술 리포트로 다시 작성하세요. "
        "모델명, 단위, 제품명, 원문 기술 표기는 필요하면 원문 그대로 유지합니다. "
        "내용을 추가로 추측하지 말고 기존 리포트의 의미를 보존하세요."
    )
    prompt = f"리포트 유형: {report_type}\n\n원문 리포트:\n{report}"
    return generate_text(instructions, prompt)


def refine_korean_analysis_report(report: str, report_type: str) -> str:
    instructions = (
        "당신은 산업 기술문서 분석 리포트를 고객사 내부 검토용 한국어 문서로 정제하는 전문 에디터입니다. "
        "원문이 어떤 언어이든 최종 결과는 한국어를 최우선으로 작성합니다. "
        "단, 모델명, 제품명, 부품명, 표준명, 단위, 수치, 도면 번호, 원문 약어는 변경하지 말고 보존합니다. "
        "영어 문장이나 영어 섹션 제목이 남아 있으면 자연스러운 한국어로 번역합니다. "
        "단순 요약이 아니라 검토자가 바로 의사결정에 쓸 수 있는 분석 리포트로 정리합니다. "
        "OCR이 불명확한 숫자나 문구는 단정하지 말고 '확인 필요'로 표시합니다. "
        "새로운 사실을 지어내지 말고 입력 리포트의 근거와 의미만 보존합니다."
    )
    prompt = (
        f"리포트 유형: {report_type}\n\n"
        "아래 초안을 한국어 우선의 정밀 분석 리포트로 다시 작성하세요.\n"
        "출력 구조:\n"
        "1. 핵심 판정\n"
        "2. 확인된 정보\n"
        "3. 분석 및 해석\n"
        "4. 불명확하거나 위험한 지점\n"
        "5. 담당자 확인 질문\n"
        "6. 다음 액션\n\n"
        f"초안:\n{report}"
    )
    return ensure_korean_first_report(generate_text(instructions, prompt), report_type)


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

    context = document_context(document_id)
    if not context:
        return {
            "document_id": document_id,
            "report": "OCR 또는 텍스트 추출 결과가 없어 정밀 분석을 실행할 수 없습니다. 먼저 OCR을 실행해 주세요.",
            "fields": extracted,
            "evidence": [],
            "engine": "none",
        }

    filename = document_filename(document_id)
    instructions = (
        "당신은 산업 설비 스펙시트 검토 전문가입니다. 설명과 분석은 한국어를 최우선으로 작성합니다. "
        "제목, 표 헤더, 항목명, 결론, 확인 질문은 가능한 한 자연스러운 한국어로 작성합니다. "
        "모델명, 단위, 원문에 적힌 장비명, 부품명, 기술 표기는 원문 표기를 유지할 수 있습니다. "
        "OCR 원문에 있는 정보만 근거로 삼고, 없는 값은 추측하지 말고 '확인 필요'로 표시합니다. "
        "숫자, 단위, 모델명, 전원, 치수, 용량, 온도 조건은 원문과 다르게 바꾸지 않습니다. "
        "결과는 전문가가 바로 검토할 수 있는 정제된 분석 리포트여야 하며, 단순 요약이나 원문 나열을 하지 않습니다."
    )
    prompt = (
        f"문서명: {filename}\n\n"
        "아래 OCR 텍스트를 바탕으로 스펙시트 정밀 분석 리포트를 작성하세요.\n\n"
        "중요: 리포트 설명은 한국어를 최우선으로 하되, 모델명/단위/제품명/원문 기술 표기는 그대로 유지해도 됩니다.\n\n"
        "반드시 다음 구조로 작성하세요:\n"
        "1. 핵심 판정\n"
        "2. 확인된 주요 사양 표: 항목 / 값 / 단위 / 근거 페이지 / 신뢰도\n"
        "3. 설치/운영 조건 해석\n"
        "4. 숫자·단위 검증 포인트\n"
        "5. 누락되었거나 불명확한 정보\n"
        "6. 담당자에게 물어봐야 할 확인 질문\n"
        "7. 리스크 및 다음 액션\n\n"
        "신뢰도는 '높음/중간/낮음'으로 표시하세요. OCR이 깨진 값은 후보를 단정하지 말고 확인 질문으로 빼세요.\n\n"
        f"OCR 텍스트:\n{context}"
    )
    report = refine_korean_analysis_report(
        generate_text(instructions, prompt),
        "스펙시트 정밀 분석",
    )
    return {
        "document_id": document_id,
        "report": report,
        "fields": extracted,
        "evidence": evidence_candidates(
            document_id,
            [
                "model capacity dimensions power voltage temperature manufacturer",
                "installation space clearance foundation utility",
                "kg mm kw hp rpm hz dimensions capacity",
            ],
        ),
        "engine": "openai",
    }


def review_layout(document_id: int) -> dict[str, object]:
    checks = [
        (
            "설치 공간 및 서비스 여유",
            "required installation footprint service clearance 설치 공간 서비스 여유",
            "장비 설치 면적과 유지보수 접근 공간을 확인합니다.",
        ),
        (
            "천장 높이 조건",
            "ceiling height layout notes 천장 높이",
            "천장 높이와 상부 간섭 가능성을 확인합니다.",
        ),
        (
            "유틸리티 연결 조건",
            "power air fuel exhaust ventilation utility 전원 공압 연료 배기 환기",
            "전원, 공압, 연료, 배기, 환기 등 유틸리티 조건을 확인합니다.",
        ),
        (
            "누락 또는 불명확한 치수",
            "missing unclear dimensions engineer review 치수 간격 확인",
            "OCR 또는 도면에서 불명확한 치수와 현장 확인이 필요한 항목을 찾습니다.",
        ),
    ]
    evidence = [
        {
            "name": name,
            "instruction": description,
            "evidence": [
                {
                    "page_number": hit.page_number,
                    "excerpt": hit.content[:500],
                    "score": round(hit.score, 4),
                }
                for hit in search(query, document_id=document_id, limit=2)
            ],
        }
        for name, query, description in checks
    ]
    context = document_context(document_id)
    if not context:
        return {
            "document_id": document_id,
            "notice": "OCR 또는 텍스트 추출 결과가 없어 레이아웃 정밀 검토를 실행할 수 없습니다.",
            "report": "먼저 OCR을 실행한 뒤 다시 Layout Check를 실행해 주세요.",
            "checks": evidence,
            "engine": "none",
        }

    filename = document_filename(document_id)
    instructions = (
        "당신은 산업 설비 배치도와 설치 레이아웃을 검토하는 기술 검토자입니다. 설명과 분석은 한국어를 최우선으로 작성합니다. "
        "제목, 표 헤더, 항목명, 결론, 확인 질문은 가능한 한 자연스러운 한국어로 작성합니다. "
        "모델명, 단위, 도면 표기, 장비명, 기술 표기는 원문 표기를 유지할 수 있습니다. "
        "OCR 원문과 도면 텍스트에 있는 내용만 근거로 삼고, 도면에서 확인되지 않는 내용은 추측하지 않습니다. "
        "문제점 후보, 누락 정보, 현장 확인 질문을 분리해서 제시합니다. 최종 승인은 엔지니어가 해야 함을 명시합니다."
    )
    prompt = (
        f"문서명: {filename}\n\n"
        "아래 OCR 텍스트를 바탕으로 레이아웃/설치 검토 리포트를 작성하세요.\n\n"
        "중요: 리포트 설명은 한국어를 최우선으로 하되, 모델명/단위/도면 표기/원문 기술 표기는 그대로 유지해도 됩니다.\n\n"
        "반드시 다음 구조로 작성하세요:\n"
        "1. 전체 판정\n"
        "2. 확인된 배치/설치 조건\n"
        "3. 문제점 후보 체크리스트: 항목 / 문제 가능성 / 근거 페이지 / 심각도 / 확인 방법\n"
        "4. 치수·간격·동선·서비스 공간 검토\n"
        "5. 전원/배관/배기/환기/접근성 등 유틸리티 검토\n"
        "6. 누락되었거나 읽기 어려운 정보\n"
        "7. 현장 담당자 또는 엔지니어에게 물어볼 질문\n"
        "8. 다음 액션\n\n"
        "심각도는 '높음/중간/낮음'으로 표시하세요. OCR이 불명확하면 단정하지 말고 확인 필요로 표시하세요.\n\n"
        f"OCR 텍스트:\n{context}"
    )
    report = refine_korean_analysis_report(
        generate_text(instructions, prompt),
        "레이아웃 문제점 검토",
    )
    return {
        "document_id": document_id,
        "notice": "AI 분석 결과입니다. 최종 승인과 안전 판단은 반드시 자격 있는 엔지니어가 수행해야 합니다.",
        "report": report,
        "checks": evidence,
        "engine": "openai",
    }


def translation_workflow(text: str, target_language: str) -> dict[str, str]:
    return {
        "target_language": target_language,
        "step_1_initial_translation": "Pending LLM provider integration.",
        "step_2_technical_review": "Terminology memory review should run here.",
        "step_3_language_review": "Native-language fluency review should run here.",
        "step_4_final_consolidation": text,
    }
