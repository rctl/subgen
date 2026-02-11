from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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


def transcribe_media(
    media_path: Path,
    endpoint: str,
    language: str,
    api_key: Optional[str] = None,
    chunk_seconds: int = 30,
    overlap_seconds: int = 3,
    sample_rate: int = 16000,
    timeout: int = 120,
    progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> List[Dict[str, object]]:
    from .media import start_ffmpeg_pcm
    from .subtitles import normalize_text

    bytes_per_sample = 2
    chunk_bytes = sample_rate * bytes_per_sample * chunk_seconds
    overlap_bytes = sample_rate * bytes_per_sample * overlap_seconds

    process = start_ffmpeg_pcm(str(media_path), sample_rate)
    if not process.stdout:
        raise RuntimeError("ffmpeg did not provide stdout.")
    total_chunks = _estimate_total_chunks(media_path, chunk_seconds)

    def read_chunk(reader, size: int) -> bytes:
        remaining = size
        chunks: List[bytes] = []
        while remaining > 0:
            data = reader.read(remaining)
            if not data:
                break
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    segments: List[Dict[str, object]] = []
    chunk_index = 0
    overlap_tail = b""
    last_norm = ""
    last_end = 0.0

    while True:
        if should_cancel and should_cancel():
            process.kill()
            process.communicate()
            raise RuntimeError("Job canceled.")

        chunk = read_chunk(process.stdout, chunk_bytes)
        if not chunk:
            break

        payload = overlap_tail + chunk if overlap_tail else chunk
        overlap_used = overlap_seconds if overlap_tail else 0
        offset = max(chunk_index * chunk_seconds - overlap_used, 0)

        if progress_callback:
            percent = int((chunk_index + 1) * 100 / total_chunks) if total_chunks > 0 else 0
            progress_callback(
                {
                    "stage": "transcribe",
                    "chunk_index": chunk_index + 1,
                    "total_chunks": total_chunks,
                    "progress_percent": min(100, percent),
                    "processed_seconds": (chunk_index + 1) * chunk_seconds,
                }
            )

        result = transcribe_pcm(
            endpoint,
            payload,
            sample_rate,
            language=language,
            api_key=api_key,
            timeout=timeout,
        )
        for seg in result.get("segments", []):
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", 0.0))
            text = str(seg.get("text", "")).strip()
            if not text or end <= 0:
                continue
            if end <= overlap_used:
                continue
            if start < overlap_used:
                start = overlap_used
            if end <= start:
                continue
            prepared = {"start": start + offset, "end": end + offset, "text": text}
            norm = normalize_text(prepared["text"])
            if norm and norm == last_norm and prepared["start"] <= last_end + 0.1:
                continue
            segments.append(prepared)
            last_norm = norm
            last_end = float(prepared["end"])

        overlap_tail = chunk[-overlap_bytes:] if overlap_bytes and len(chunk) >= overlap_bytes else chunk
        chunk_index += 1

    process.communicate()
    if process.returncode not in (0, None):
        raise RuntimeError("ffmpeg failed during transcription.")
    return segments


def _estimate_total_chunks(media_path: Path, chunk_seconds: int) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        payload = json.loads(output.decode("utf-8", errors="ignore"))
        duration = float(payload.get("format", {}).get("duration", 0.0))
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError, TypeError):
        return 0
    if duration <= 0:
        return 0
    return max(1, int(math.ceil(duration / max(chunk_seconds, 1))))
