from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List

from .config import Config
from .media import start_ffmpeg_pcm
from .subtitles import format_srt, normalize_text, parse_srt
from .transcribe import transcribe_pcm
from .translate import translate_segments


BYTES_PER_SAMPLE = 2  # s16le


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="Generate SRT subtitles from a video file.")
    parser.add_argument("--input", required=True, help="Path to input video file.")
    parser.add_argument("--output", required=True, help="Path to output SRT file.")
    parser.add_argument("--endpoint", required=True, help="STT server base URL.")
    parser.add_argument("--api-key", default=None, help="Optional API key for STT server.")
    parser.add_argument("--lang", default="en", help="Language code for transcription (default: en).")
    parser.add_argument(
        "--translate-to",
        default=None,
        help="Optional target language code for translation (e.g., sv, zh).",
    )
    parser.add_argument(
        "--translate-from",
        default=None,
        help="Optional source language code for translation (e.g., en).",
    )
    parser.add_argument(
        "--google-api-key",
        default=None,
        help="Google Translate API key (or set GOOGLE_TRANSLATE_API_KEY).",
    )
    parser.add_argument(
        "--translate-batch-size",
        type=int,
        default=30,
        help="Number of subtitle lines per translation batch.",
    )
    parser.add_argument(
        "--force-stt",
        action="store_true",
        help="Force STT even if the original SRT already exists.",
    )
    parser.add_argument("--chunk-seconds", type=int, default=30, help="Chunk size in seconds.")
    parser.add_argument("--overlap-seconds", type=int, default=3, help="Overlap size in seconds.")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Audio sample rate.")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds.")
    args = parser.parse_args()

    if args.chunk_seconds <= 0:
        parser.error("--chunk-seconds must be > 0")
    if args.overlap_seconds < 0:
        parser.error("--overlap-seconds must be >= 0")
    if args.overlap_seconds >= args.chunk_seconds:
        parser.error("--overlap-seconds must be smaller than --chunk-seconds")

    return Config(
        input_path=args.input,
        output_path=args.output,
        endpoint=args.endpoint,
        api_key=args.api_key,
        language=args.lang,
        translate_to=args.translate_to,
        translate_from=args.translate_from,
        google_api_key=args.google_api_key,
        translate_batch_size=args.translate_batch_size,
        force_stt=args.force_stt,
        chunk_seconds=args.chunk_seconds,
        overlap_seconds=args.overlap_seconds,
        sample_rate=args.sample_rate,
        timeout=args.timeout,
    )


def _segment_from_result(segment: Dict[str, object], offset: float, overlap_seconds: float) -> Dict[str, object] | None:
    start = float(segment.get("start", 0.0))
    end = float(segment.get("end", 0.0))
    text = str(segment.get("text", "")).strip()
    if not text or end <= 0:
        return None
    if end <= overlap_seconds:
        return None
    if start < overlap_seconds:
        start = overlap_seconds
    if end <= start:
        return None
    return {
        "start": start + offset,
        "end": end + offset,
        "text": text,
    }


def _read_chunk(reader, size: int) -> bytes:
    remaining = size
    chunks: List[bytes] = []
    while remaining > 0:
        data = reader.read(remaining)
        if not data:
            break
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def _translation_output_path(output_path: str, target_lang: str) -> str:
    if output_path.lower().endswith(".srt"):
        base = output_path[:-4]
        return f"{base}.{target_lang}.srt"
    return f"{output_path}.{target_lang}.srt"


def main() -> int:
    config = parse_args()
    if not os.path.exists(config.input_path):
        print(f"Input file not found: {config.input_path}", file=sys.stderr)
        return 1
    if config.translate_batch_size <= 0:
        print("--translate-batch-size must be > 0", file=sys.stderr)
        return 1

    segments: List[Dict[str, object]] = []
    if os.path.exists(config.output_path) and not config.force_stt:
        with open(config.output_path, "r", encoding="utf-8") as handle:
            segments = parse_srt(handle.read())
        if not segments:
            print("Existing SRT was empty; rerun with --force-stt.", file=sys.stderr)
            return 1
    else:
        chunk_bytes = config.sample_rate * BYTES_PER_SAMPLE * config.chunk_seconds
        overlap_bytes = config.sample_rate * BYTES_PER_SAMPLE * config.overlap_seconds

        process = start_ffmpeg_pcm(config.input_path, config.sample_rate)
        if not process.stdout:
            print("ffmpeg did not provide stdout.", file=sys.stderr)
            return 1

        chunk_index = 0
        overlap_tail = b""
        last_norm = ""
        last_end = 0.0

        while True:
            chunk = _read_chunk(process.stdout, chunk_bytes)
            if not chunk:
                break

            payload = overlap_tail + chunk if overlap_tail else chunk
            overlap_used = config.overlap_seconds if overlap_tail else 0
            offset = max(chunk_index * config.chunk_seconds - overlap_used, 0)

            result = transcribe_pcm(
                config.endpoint,
                payload,
                config.sample_rate,
                language=config.language,
                api_key=config.api_key,
                timeout=config.timeout,
            )
            for seg in result.get("segments", []):
                prepared = _segment_from_result(seg, offset, overlap_used)
                if not prepared:
                    continue
                norm = normalize_text(str(prepared["text"]))
                if norm and norm == last_norm and prepared["start"] <= last_end + 0.1:
                    continue
                segments.append(prepared)
                last_norm = norm
                last_end = float(prepared["end"])

            overlap_tail = (
                chunk[-overlap_bytes:] if overlap_bytes and len(chunk) >= overlap_bytes else chunk
            )
            chunk_index += 1

        stdout, stderr = process.communicate()
        if process.returncode not in (0, None):
            detail = (stderr or b"").decode("utf-8", errors="ignore").strip()
            print(f"ffmpeg failed: {detail}", file=sys.stderr)
            return 1

        srt = format_srt(segments)
        os.makedirs(os.path.dirname(config.output_path) or ".", exist_ok=True)
        with open(config.output_path, "w", encoding="utf-8") as handle:
            handle.write(srt)
        print(f"Wrote {len(segments)} segments to {config.output_path}")

    if config.translate_to:
        api_key = config.google_api_key or os.getenv("GOOGLE_TRANSLATE_API_KEY")
        translated = translate_segments(
            segments,
            config.translate_to,
            api_key=api_key,
            source_language=config.translate_from,
            batch_size=config.translate_batch_size,
            timeout=config.timeout,
        )
        translated_srt = format_srt(translated)
        translated_path = _translation_output_path(config.output_path, config.translate_to)
        os.makedirs(os.path.dirname(translated_path) or ".", exist_ok=True)
        with open(translated_path, "w", encoding="utf-8") as handle:
            handle.write(translated_srt)
        print(f"Wrote translated SRT to {translated_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
