from __future__ import annotations

from typing import Dict, Iterable, List, Optional

import requests


GOOGLE_TRANSLATE_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"


def translate_segments(
    segments: Iterable[Dict[str, object]],
    target_language: str,
    api_key: str,
    source_language: Optional[str] = None,
    batch_size: int = 30,
    timeout: int = 120,
) -> List[Dict[str, object]]:
    if not api_key:
        raise ValueError("Google Translate API key is required for translation.")

    segment_list = list(segments)
    translated: List[Dict[str, object]] = []
    for start in range(0, len(segment_list), batch_size):
        batch = segment_list[start : start + batch_size]
        texts = [str(item.get("text", "")).strip() for item in batch]
        translated_texts = _translate_batch(
            texts,
            target_language,
            api_key,
            source_language=source_language,
            timeout=timeout,
        )
        if len(translated_texts) != len(batch):
            translated_texts = []
            for line in texts:
                translated_texts.append(
                    _translate_single(
                        line,
                        target_language,
                        api_key,
                        source_language=source_language,
                        timeout=timeout,
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
    source_language: Optional[str],
    timeout: int,
) -> List[str]:
    payload = {"q": texts, "target": target_language, "format": "text"}
    if source_language:
        payload["source"] = source_language
    headers = {"content-type": "application/json"}
    response = requests.post(
        GOOGLE_TRANSLATE_ENDPOINT,
        headers=headers,
        params={"key": api_key},
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Google Translate error {response.status_code}: {response.text}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Google Translate response was not a JSON object.")
    translations = data.get("data", {}).get("translations", [])
    if not isinstance(translations, list):
        raise RuntimeError("Google Translate response missing translations.")
    return [str(item.get("translatedText", "")).strip() for item in translations]


def _translate_single(
    text: str,
    target_language: str,
    api_key: str,
    source_language: Optional[str],
    timeout: int,
) -> str:
    if not text:
        return ""
    translated = _translate_batch(
        [text],
        target_language,
        api_key,
        source_language=source_language,
        timeout=timeout,
    )
    return translated[0] if translated else ""
