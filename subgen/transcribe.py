from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
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
    vad_threshold = 0.30
    vad_frame_ms = 30
    vad_padding_ms = 450
    vad_min_speech_ms = 180
    vad_min_gap_ms = 220

    try:
        from openwakeword.vad import VAD

        vad_model = VAD()
    except Exception as exc:
        vad_model = None
        print(f"[subgen][vad] disabled ({exc})", flush=True)

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

        regions = _compute_regions_from_vad(
            vad_model=vad_model,
            pcm_bytes=payload,
            sample_rate=sample_rate,
            threshold=vad_threshold,
            frame_ms=vad_frame_ms,
            padding_ms=vad_padding_ms,
            min_speech_ms=vad_min_speech_ms,
            min_gap_ms=vad_min_gap_ms,
        )
        if vad_model is None:
            regions = [{"start": 0.0, "end": len(payload) / float(sample_rate * bytes_per_sample), "max_score": 1.0}]

        if regions:
            max_score = max(float(r.get("max_score", 0.0)) for r in regions)
        else:
            max_score = 0.0
        print(
            f"[subgen][vad] chunk={chunk_index + 1} threshold={vad_threshold:.2f} regions={len(regions)} max={max_score:.3f}",
            flush=True,
        )

        for region_index, region in enumerate(regions, start=1):
            region_start = float(region["start"])
            region_end = float(region["end"])
            if region_end <= region_start:
                continue

            start_byte = int(region_start * sample_rate * bytes_per_sample)
            end_byte = int(region_end * sample_rate * bytes_per_sample)
            start_byte = max(0, min(start_byte, len(payload)))
            end_byte = max(start_byte, min(end_byte, len(payload)))
            if end_byte - start_byte < sample_rate * bytes_per_sample * 0.1:
                continue

            region_payload = payload[start_byte:end_byte]
            print(
                f"[subgen][vad] chunk={chunk_index + 1} region={region_index} start={region_start:.3f}s end={region_end:.3f}s bytes={len(region_payload)} score={float(region.get('max_score', 0.0)):.3f}",
                flush=True,
            )

            result = transcribe_pcm(
                endpoint,
                region_payload,
                sample_rate,
                language=language,
                api_key=api_key,
                timeout=timeout,
            )
            for seg in result.get("segments", []):
                start = float(seg.get("start", 0.0)) + region_start
                end = float(seg.get("end", 0.0)) + region_start
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


def _compute_regions_from_vad(
    vad_model: object,
    pcm_bytes: bytes,
    sample_rate: int,
    threshold: float,
    frame_ms: int,
    padding_ms: int,
    min_speech_ms: int,
    min_gap_ms: int,
) -> List[Dict[str, float]]:
    if vad_model is None or sample_rate <= 0:
        return []

    frame_samples = int(sample_rate * frame_ms / 1000)
    if frame_samples <= 0:
        return []
    frame_bytes = frame_samples * 2
    if frame_bytes <= 0:
        return []

    usable = (len(pcm_bytes) // frame_bytes) * frame_bytes
    if usable <= 0:
        return []

    pcm = np.frombuffer(pcm_bytes[:usable], dtype=np.int16)
    total_frames = usable // frame_bytes
    scores: List[float] = []
    for i in range(total_frames):
        start = i * frame_samples
        end = start + frame_samples
        frame = pcm[start:end]
        score = float(vad_model.predict(frame, frame_size=frame_samples))
        scores.append(score)

    active: List[Dict[str, float]] = []
    current_start: Optional[int] = None
    current_max = 0.0
    for i, score in enumerate(scores):
        if score >= threshold:
            if current_start is None:
                current_start = i
                current_max = score
            else:
                current_max = max(current_max, score)
        elif current_start is not None:
            active.append({"start_frame": float(current_start), "end_frame": float(i), "max_score": float(current_max)})
            current_start = None
            current_max = 0.0
    if current_start is not None:
        active.append(
            {"start_frame": float(current_start), "end_frame": float(total_frames), "max_score": float(current_max)}
        )

    if not active:
        return []

    min_gap_frames = max(1, int(min_gap_ms / frame_ms))
    merged: List[Dict[str, float]] = []
    for r in active:
        if not merged:
            merged.append(dict(r))
            continue
        prev = merged[-1]
        gap = int(r["start_frame"] - prev["end_frame"])
        if gap <= min_gap_frames:
            prev["end_frame"] = r["end_frame"]
            prev["max_score"] = max(float(prev["max_score"]), float(r["max_score"]))
        else:
            merged.append(dict(r))

    pad_frames = max(0, int(padding_ms / frame_ms))
    min_speech_frames = max(1, int(min_speech_ms / frame_ms))
    regions: List[Dict[str, float]] = []
    for r in merged:
        start_frame = max(0, int(r["start_frame"]) - pad_frames)
        end_frame = min(total_frames, int(r["end_frame"]) + pad_frames)
        if end_frame - start_frame < min_speech_frames:
            continue
        start_s = (start_frame * frame_samples) / float(sample_rate)
        end_s = (end_frame * frame_samples) / float(sample_rate)
        regions.append({"start": start_s, "end": end_s, "max_score": float(r["max_score"])})

    return regions
