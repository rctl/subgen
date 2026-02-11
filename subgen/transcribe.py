from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    speech_intervals = _detect_speech_intervals(media_path)

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
            if speech_intervals:
                overlap, aligned_start, aligned_end = _speech_overlap(start + offset, end + offset, speech_intervals)
                if overlap <= 0:
                    continue
                start = max(aligned_start - offset, 0.0)
                end = max(aligned_end - offset, start + 0.01)
            prepared = {"start": start + offset, "end": end + offset, "text": text}
            norm = normalize_text(prepared["text"])
            if not _is_plausible_text(prepared["text"], language):
                continue
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


def _detect_speech_intervals(media_path: Path) -> List[Tuple[float, float]]:
    cmd = [
        "ffmpeg",
        "-i",
        str(media_path),
        "-af",
        "silencedetect=noise=-38dB:d=0.35",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False)
    except OSError:
        return []
    log = proc.stderr or ""
    duration = _estimate_duration_seconds(media_path)
    if duration <= 0:
        return []
    silence_intervals = _parse_silence_intervals(log, duration)
    if not silence_intervals:
        return [(0.0, duration)]
    speech: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in silence_intervals:
        if start > cursor:
            speech.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        speech.append((cursor, duration))
    return [(max(0.0, s), max(0.0, e)) for s, e in speech if e - s >= 0.08]


def _estimate_duration_seconds(media_path: Path) -> float:
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
        return float(payload.get("format", {}).get("duration", 0.0))
    except (subprocess.CalledProcessError, json.JSONDecodeError, ValueError, TypeError):
        return 0.0


def _parse_silence_intervals(log: str, duration: float) -> List[Tuple[float, float]]:
    starts = [float(v) for v in re.findall(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", log)]
    ends = [float(v) for v in re.findall(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)", log)]
    intervals: List[Tuple[float, float]] = []
    i = 0
    j = 0
    while i < len(starts) or j < len(ends):
        if i < len(starts):
            s = starts[i]
            i += 1
        else:
            s = 0.0
        if j < len(ends):
            e = ends[j]
            j += 1
        else:
            e = duration
        if e > s:
            intervals.append((s, e))
    return intervals


def _speech_overlap(start: float, end: float, speech_intervals: List[Tuple[float, float]]) -> Tuple[float, float, float]:
    overlap = 0.0
    first: Optional[float] = None
    last: Optional[float] = None
    for s, e in speech_intervals:
        if e <= start:
            continue
        if s >= end:
            break
        left = max(start, s)
        right = min(end, e)
        if right <= left:
            continue
        overlap += right - left
        if first is None:
            first = left
        last = right
    if first is None or last is None:
        return 0.0, start, end
    return overlap, first, last


def _is_plausible_text(text: str, language: str) -> bool:
    t = " ".join(text.strip().split())
    if len(t) < 2:
        return False
    if re.search(r"(.)\1\1\1\1", t):
        return False
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", t))
    alpha_count = len(re.findall(r"[A-Za-z\u00C0-\u024F\u00C5\u00C4\u00D6\u00E5\u00E4\u00F6]", t))
    alnum_like = cjk_count + alpha_count + len(re.findall(r"[0-9]", t))
    symbol_count = len(re.findall(r"[^\w\s\u4e00-\u9fff]", t))
    if symbol_count > alnum_like and len(t) > 6:
        return False
    lang_norm = (language or "").lower()
    if lang_norm.startswith("zh"):
        return cjk_count > 0 or alpha_count >= 2
    if lang_norm.startswith("en") or lang_norm.startswith("sv"):
        return alpha_count >= 2
    return (cjk_count + alpha_count) >= 2
