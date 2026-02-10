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
