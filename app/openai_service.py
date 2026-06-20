from __future__ import annotations

import httpx

from app.config import settings


class OpenAIUnavailable(RuntimeError):
    pass


def generate_text(instructions: str, prompt: str) -> str:
    if not settings.openai_api_key:
        raise OpenAIUnavailable("OPENAI_API_KEY is not configured.")

    response = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.openai_model,
            "instructions": instructions,
            "input": prompt,
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("output_text"):
        return payload["output_text"].strip()

    parts: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        raise OpenAIUnavailable("OpenAI response did not include output text.")
    return text
