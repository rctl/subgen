from __future__ import annotations

import json
import re
from typing import Callable, Dict, Iterable, List, Optional

import requests


GOOGLE_TRANSLATE_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"
ANTHROPIC_MESSAGES_ENDPOINT = "https://api.anthropic.com/v1/messages"


def translate_segments(
    segments: Iterable[Dict[str, object]],
    target_language: str,
    api_key: str,
    source_language: Optional[str] = None,
    batch_size: int = 30,
    timeout: int = 120,
    progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[Dict[str, object]]:
    if not api_key:
        raise ValueError("Google Translate API key is required for translation.")

    segment_list = list(segments)
    translated: List[Dict[str, object]] = []
    for start in range(0, len(segment_list), batch_size):
        if should_cancel and should_cancel():
            raise RuntimeError("Job canceled.")
        batch = segment_list[start : start + batch_size]
        if progress_callback:
            progress_callback(
                {
                    "stage": "translate",
                    "processed_segments": min(start, len(segment_list)),
                    "total_segments": len(segment_list),
                }
            )
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
    if progress_callback:
        progress_callback(
            {
                "stage": "translate",
                "processed_segments": len(segment_list),
                "total_segments": len(segment_list),
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


def translate_segments_anthropic(
    segments: Iterable[Dict[str, object]],
    target_language: str,
    api_key: str,
    model: str,
    source_language: Optional[str] = None,
    batch_size: int = 200,
    timeout: int = 180,
    progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[Dict[str, object]]:
    if not api_key:
        raise ValueError("Anthropic API key is required for Anthropic translation.")
    if not model:
        raise ValueError("Anthropic model is required for Anthropic translation.")

    segment_list = list(segments)
    translated: List[Dict[str, object]] = []
    for start in range(0, len(segment_list), batch_size):
        if should_cancel and should_cancel():
            raise RuntimeError("Job canceled.")
        batch = segment_list[start : start + batch_size]
        if progress_callback:
            progress_callback(
                {
                    "stage": "translate",
                    "processed_segments": min(start, len(segment_list)),
                    "total_segments": len(segment_list),
                }
            )
        texts = [str(item.get("text", "")).strip() for item in batch]
        translated_texts = _anthropic_translate_batch(
            texts=texts,
            target_language=target_language,
            api_key=api_key,
            model=model,
            source_language=source_language,
            timeout=timeout,
        )
        expected_count = len(batch)
        actual_count = len(translated_texts)
        if actual_count != expected_count:
            print(
                f"[subgen][translate] anthropic count mismatch: expected={expected_count} got={actual_count}; applying trim/pad",
                flush=True,
            )
            if actual_count > expected_count:
                translated_texts = translated_texts[:expected_count]
            else:
                missing = expected_count - actual_count
                translated_texts = translated_texts + texts[-missing:]
        for original, new_text in zip(batch, translated_texts):
            translated.append(
                {
                    "start": float(original.get("start", 0.0)),
                    "end": float(original.get("end", 0.0)),
                    "text": new_text,
                }
            )
    if progress_callback:
        progress_callback(
            {
                "stage": "translate",
                "processed_segments": len(segment_list),
                "total_segments": len(segment_list),
            }
        )
    return translated


def _anthropic_translate_batch(
    texts: List[str],
    target_language: str,
    api_key: str,
    model: str,
    source_language: Optional[str],
    timeout: int,
) -> List[str]:
    try:
        from anthropic import Anthropic
    except Exception as exc:
        raise RuntimeError(f"Anthropic SDK is required for Anthropic translation: {exc}") from exc

    system = (
        "You are a subtitle translation engine. Translate each input subtitle line to the target language. "
        "Keep line order and count identical. Do not merge or split entries. Return only valid JSON."
    )
    source_hint = source_language or "auto-detect"
    user_prompt = (
        f"Source language: {source_hint}\n"
        f"Target language: {target_language}\n"
        "Return exactly this schema as JSON:\n"
        '{"translations":["..."]}\n'
        "Input lines JSON:\n"
        + json.dumps(texts, ensure_ascii=False)
    )
    client = Anthropic(api_key=api_key, timeout=timeout)
    response = client.messages.create(
        model=model,
        max_tokens=max(1024, len(texts) * 48),
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    content = getattr(response, "content", None)
    if not isinstance(content, list):
        raise RuntimeError("Anthropic response missing content blocks.")
    text_blocks: List[str] = []
    for block in content:
        block_type = getattr(block, "type", "")
        block_text = getattr(block, "text", "")
        if block_type == "text":
            text_blocks.append(str(block_text))
    joined = "\n".join(text_blocks).strip()
    if not joined:
        raise RuntimeError("Anthropic response did not include translation text.")
    payload_json = _extract_json_object(joined)
    translations = payload_json.get("translations", [])
    if not isinstance(translations, list):
        raise RuntimeError("Anthropic response missing translations array.")
    return [str(item).strip() for item in translations]


def _extract_json_object(text: str) -> Dict[str, object]:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(f"Anthropic response did not contain valid JSON: {exc}") from exc
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc2:
            raise RuntimeError(f"Anthropic response did not contain valid JSON: {exc2}") from exc2
    if not isinstance(payload, dict):
        raise RuntimeError("Anthropic JSON payload is not an object.")
    return payload
