from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa", ".sub"}


def scan_media(base_dir: str) -> List[Dict[str, object]]:
    base_path = Path(base_dir).resolve()
    items: List[Dict[str, object]] = []
    for root, _, files in os.walk(base_path):
        for name in files:
            path = Path(root) / name
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            items.append(describe_media(path))
    return sorted(items, key=lambda item: str(item.get("title", "")).lower())


def describe_media(path: Path) -> Dict[str, object]:
    embedded = probe_embedded_subs(path)
    sidecar = find_sidecar_subs(path)
    title = path.stem
    return {
        "id": _hash_id(str(path)),
        "path": str(path),
        "title": title,
        "embedded_subs": embedded,
        "sidecar_subs": sidecar,
        "has_subs": bool(embedded or sidecar),
    }


def probe_embedded_subs(path: Path) -> List[Dict[str, object]]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError:
        return []
    try:
        payload = json.loads(output.decode("utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return []
    subs: List[Dict[str, object]] = []
    for stream in payload.get("streams", []):
        index = stream.get("index")
        tags = stream.get("tags", {}) or {}
        lang = tags.get("language") or "und"
        title = tags.get("title") or "Embedded subtitle"
        subs.append(
            {
                "id": f"embedded:{index}",
                "lang": lang,
                "title": title,
                "stream_index": index,
                "kind": "embedded",
            }
        )
    return subs


def find_sidecar_subs(path: Path) -> List[Dict[str, object]]:
    base = path.stem
    directory = path.parent
    subs: List[Dict[str, object]] = []
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if not entry.name.startswith(base):
            continue
        lang = _lang_from_filename(base, entry.name)
        subs.append(
            {
                "id": f"sidecar:{entry}",
                "lang": lang or "und",
                "title": entry.name,
                "path": str(entry),
                "format": entry.suffix.lower().lstrip("."),
                "kind": "sidecar",
            }
        )
    return subs


def extract_embedded_sub(path: Path, stream_index: int, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-map",
        f"0:{stream_index}",
        "-c:s",
        "srt",
        str(output_path),
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def resolve_media_path(base_dir: str, requested: Optional[str]) -> str:
    base_path = Path(base_dir).resolve()
    if not requested:
        return str(base_path)
    requested_path = Path(requested).resolve()
    if base_path not in requested_path.parents and base_path != requested_path:
        raise ValueError("Requested path must be within base directory.")
    return str(requested_path)


def _hash_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _lang_from_filename(base: str, filename: str) -> Optional[str]:
    if not filename.startswith(base):
        return None
    remainder = filename[len(base) :].lstrip(".- _")
    if not remainder:
        return None
    parts = remainder.split(".")
    if not parts:
        return None
    candidate = parts[0]
    candidate = candidate.replace("gen_", "")
    return candidate or None
