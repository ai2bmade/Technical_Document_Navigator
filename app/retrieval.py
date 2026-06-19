from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.storage import db


STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "for",
    "from",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "when",
    "where",
    "with",
}


@dataclass(frozen=True)
class Hit:
    document_id: int
    filename: str
    page_number: int
    section_title: str | None
    content: str
    score: float


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9가-힣_+-]{2,}", text.lower())
    return [token for token in tokens if token not in STOPWORDS]


def score(query: str, content: str) -> float:
    query_terms = Counter(tokenize(query))
    content_terms = Counter(tokenize(content))
    if not query_terms or not content_terms:
        return 0.0
    total = 0.0
    for term, query_count in query_terms.items():
        if term in content_terms:
            total += query_count * (1.0 + math.log(content_terms[term]))
    return total / max(1.0, math.sqrt(sum(content_terms.values())))


def search(query: str, document_id: int | None = None, limit: int = 5) -> list[Hit]:
    sql = """
        select c.document_id, d.filename, c.page_number, c.section_title, c.content
        from chunks c
        join documents d on d.id = c.document_id
    """
    params: tuple[object, ...] = ()
    if document_id is not None:
        sql += " where c.document_id = ?"
        params = (document_id,)

    hits: list[Hit] = []
    with db() as conn:
        for row in conn.execute(sql, params):
            value = score(query, row["content"])
            if value > 0:
                hits.append(
                    Hit(
                        document_id=row["document_id"],
                        filename=row["filename"],
                        page_number=row["page_number"],
                        section_title=row["section_title"],
                        content=row["content"],
                        score=value,
                    )
                )
    return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]
