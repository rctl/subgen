from __future__ import annotations

import json
from typing import Any, Dict, Optional

import requests


def _normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/transcribe"):
        return endpoint
    return f"{endpoint}/transcribe"


def transcribe_pcm(
    endpoint: str,
    pcm_bytes: bytes,
    sample_rate: int,
    language: str = "en",
    api_key: Optional[str] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    url = _normalize_endpoint(endpoint)
    headers = {
        "X-Sample-Rate": str(sample_rate),
        "X-Lang": language,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.post(url, headers=headers, data=pcm_bytes, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"STT error {response.status_code}: {response.text}")

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"STT returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("STT response was not a JSON object.")
    return payload
