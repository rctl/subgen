from __future__ import annotations

import shutil
import subprocess
from typing import List


def _ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg is not available in PATH.")
    return path


def build_ffmpeg_pcm_cmd(input_path: str, sample_rate: int) -> List[str]:
    return [
        _ffmpeg_path(),
        "-v",
        "error",
        "-i",
        input_path,
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]


def start_ffmpeg_pcm(input_path: str, sample_rate: int) -> subprocess.Popen:
    cmd = build_ffmpeg_pcm_cmd(input_path, sample_rate)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
