from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    input_path: str
    output_path: str
    endpoint: str
    api_key: Optional[str]
    language: str
    translate_to: Optional[str]
    translate_from: Optional[str]
    google_api_key: Optional[str]
    translate_batch_size: int
    force_stt: bool
    chunk_seconds: int
    overlap_seconds: int
    sample_rate: int
    timeout: int
