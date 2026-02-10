from __future__ import annotations

import json
from typing import Dict, Iterable, List

import requests


ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"


def translate_segments(
    segments: Iterable[Dict[str, object]],
    target_language: str,
    api_key: str,
    model: str = "claude-3-haiku-20240307",
    batch_size: int = 30,
    timeout: int = 120,
) -> List[Dict[str, object]]:
    if not api_key:
        raise ValueError("Anthropic API key is required for translation.")

    segment_list = list(segments)
    translated: List[Dict[str, object]] = []
    for start in range(0, len(segment_list), batch_size):
        batch = segment_list[start : start + batch_size]
        texts = [str(item.get("text", "")).strip() for item in batch]
        translated_texts = _translate_batch(
            texts, target_language, api_key, model=model, timeout=timeout
        )
        if len(translated_texts) != len(batch):
            translated_texts = []
            for line in texts:
                translated_texts.append(
                    _translate_single(
                        line, target_language, api_key, model=model, timeout=timeout
                    )
                )
        for original, new_text in zip(batch, translated_texts):
            translated.append(
                {
                    "start": float(original.get("start", 0.0)),
                    "end": float(original.get("end", 0.0)),
                    "text": new_text,
                }
            )
    return translated


def _translate_batch(
    texts: List[str],
    target_language: str,
    api_key: str,
    model: str,
    timeout: int,
) -> List[str]:
    prompt = {
        "role": "user",
        "content": (
            "Translate the following subtitle lines into "
            f"{target_language}. Preserve meaning, keep each line concise, and return "
            "only a JSON array of strings in the same order. "
            "If an input line is empty, return an empty string at that position. "
            "Do not add commentary.\n\n"
            f"{json.dumps(texts, ensure_ascii=False)}"
        ),
    }
    payload = {
        "model": model,
        "max_tokens": 2000,
        "temperature": 0.2,
        "messages": [prompt],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    response = requests.post(
        ANTHROPIC_ENDPOINT,
        headers=headers,
        data=json.dumps(payload),
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Anthropic error {response.status_code}: {response.text}")
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Anthropic returned invalid JSON: {exc}") from exc
    content = data.get("content", [])
    if not content or not isinstance(content, list):
        raise RuntimeError("Anthropic response missing content.")
    text = ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text += item.get("text", "")
    text = text.strip()
    try:
        translated = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Translation was not valid JSON: {exc}\n{text}") from exc
    if not isinstance(translated, list):
        raise RuntimeError("Translation response was not a JSON array.")
    return [str(entry).strip() for entry in translated]


def _translate_single(
    text: str,
    target_language: str,
    api_key: str,
    model: str,
    timeout: int,
) -> str:
    if not text:
        return ""
    translated = _translate_batch(
        [text], target_language, api_key, model=model, timeout=timeout
    )
    return translated[0] if translated else ""
