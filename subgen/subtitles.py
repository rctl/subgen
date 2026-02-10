from __future__ import annotations

from typing import Iterable, List, Dict


def _format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    millis = int(round(seconds * 1000.0))
    hours = millis // 3_600_000
    millis -= hours * 3_600_000
    minutes = millis // 60_000
    millis -= minutes * 60_000
    secs = millis // 1000
    millis -= secs * 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def format_srt(segments: Iterable[Dict[str, object]]) -> str:
    lines: List[str] = []
    index = 1
    for segment in segments:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", 0.0))
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        if end <= start:
            continue
        lines.append(str(index))
        lines.append(f"{_format_timestamp(start)} --> {_format_timestamp(end)}")
        lines.append(text)
        lines.append("")
        index += 1
    return "\n".join(lines).rstrip() + "\n"


def parse_srt(text: str) -> List[Dict[str, object]]:
    blocks = [block for block in text.split("\n\n") if block.strip()]
    segments: List[Dict[str, object]] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_line = lines[1]
        if "-->" not in time_line:
            continue
        start_raw, end_raw = [part.strip() for part in time_line.split("-->", 1)]
        try:
            start = _parse_timestamp(start_raw)
            end = _parse_timestamp(end_raw)
        except ValueError:
            continue
        text_lines = lines[2:] if len(lines) > 2 else []
        segment_text = "\n".join(text_lines).strip()
        if not segment_text:
            continue
        segments.append({"start": start, "end": end, "text": segment_text})
    return segments


def _parse_timestamp(value: str) -> float:
    parts = value.split(",")
    if len(parts) != 2:
        raise ValueError("Invalid timestamp")
    time_part, ms_part = parts
    hours_str, minutes_str, seconds_str = time_part.split(":")
    hours = int(hours_str)
    minutes = int(minutes_str)
    seconds = int(seconds_str)
    millis = int(ms_part)
    return hours * 3600 + minutes * 60 + seconds + (millis / 1000.0)
