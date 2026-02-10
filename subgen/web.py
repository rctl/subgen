from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory

from .library import (
    describe_media,
    extract_embedded_sub,
    resolve_media_path,
    scan_media,
)
from .media import start_ffmpeg_pcm
from .subtitles import format_srt, normalize_text, parse_srt
from .transcribe import transcribe_pcm
from .translate import translate_segments


BYTES_PER_SAMPLE = 2  # s16le


CONFIG_PATH = os.environ.get("SUBGEN_CONFIG_PATH", os.path.join(os.getcwd(), "config.json"))


def load_config() -> Dict[str, object]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def create_app(base_dir: str, stt_endpoint: str) -> Flask:
    app = Flask(__name__, static_folder="web/static")
    app.config["BASE_DIR"] = base_dir
    app.config["STT_ENDPOINT"] = stt_endpoint
    print(f"[subgen] config base_dir={base_dir} stt_endpoint={stt_endpoint}")
    print("[subgen] scanning media library...")
    app.config["MEDIA_CACHE"] = scan_media(base_dir)
    print(f"[subgen] scan complete: {len(app.config['MEDIA_CACHE'])} items")

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(app.static_folder, filename)

    @app.route("/api/media")
    def api_media():
        base_dir_value = app.config["BASE_DIR"]
        rescan = request.args.get("rescan") == "1"
        if rescan:
            print("[subgen] rescan requested")
            app.config["MEDIA_CACHE"] = scan_media(base_dir_value)
            print(f"[subgen] scan complete: {len(app.config['MEDIA_CACHE'])} items")
        items = app.config.get("MEDIA_CACHE", [])
        return jsonify({"base_dir": base_dir_value, "items": items})

    @app.route("/api/media/describe", methods=["POST"])
    def api_media_describe():
        data = request.get_json(silent=True) or {}
        media_path = data.get("path")
        if not media_path:
            return jsonify({"error": "Missing media path"}), 400
        item = describe_media(Path(media_path))
        return jsonify(item)

    @app.route("/api/subtitles/generate", methods=["POST"])
    def api_generate():
        data = request.get_json(silent=True) or {}
        media_path = data.get("media_path")
        source_lang = (data.get("source_lang") or "").strip() or "und"
        target_lang = (data.get("target_lang") or "").strip() or source_lang
        mode = (data.get("mode") or "use_existing").strip()
        existing_id = data.get("existing_sub_id")

        if not media_path:
            return jsonify({"error": "Missing media_path"}), 400

        media = describe_media(Path(media_path))
        output_dir = Path(media_path).parent
        stem = Path(media_path).stem

        def output_path_for(lang: str) -> Path:
            return output_dir / f"{stem}.gen_{lang}.srt"

        existing = _resolve_existing_sub(media, existing_id)

        if mode == "use_existing":
            if existing:
                return jsonify(
                    _generate_from_existing(
                        existing,
                        source_lang,
                        target_lang,
                        Path(media_path),
                        output_path_for,
                    )
                )
            mode = "transcribe"

        if mode == "translate_existing":
            if not existing:
                return jsonify({"error": "No existing subtitle available."}), 400
            return jsonify(
                _generate_from_existing(
                    existing,
                    source_lang,
                    target_lang,
                    Path(media_path),
                    output_path_for,
                    require_translate=True,
                )
            )

        if mode != "transcribe":
            return jsonify({"error": f"Unknown mode: {mode}"}), 400

        segments = transcribe_media(
            Path(media_path),
            app.config["STT_ENDPOINT"],
            source_lang,
        )
        source_output = output_path_for(source_lang)
        source_output.write_text(format_srt(segments), encoding="utf-8")

        outputs = [str(source_output)]
        if target_lang != source_lang:
            translated = translate_segments(
                segments,
                target_lang,
                api_key=_google_api_key(),
                source_language=source_lang if source_lang != "und" else None,
            )
            target_output = output_path_for(target_lang)
            target_output.write_text(format_srt(translated), encoding="utf-8")
            outputs.append(str(target_output))

        return jsonify({"outputs": outputs})

    return app


def transcribe_media(
    media_path: Path,
    endpoint: str,
    language: str,
    api_key: Optional[str] = None,
    chunk_seconds: int = 30,
    overlap_seconds: int = 3,
    sample_rate: int = 16000,
    timeout: int = 120,
) -> List[Dict[str, object]]:
    chunk_bytes = sample_rate * BYTES_PER_SAMPLE * chunk_seconds
    overlap_bytes = sample_rate * BYTES_PER_SAMPLE * overlap_seconds

    process = start_ffmpeg_pcm(str(media_path), sample_rate)
    if not process.stdout:
        raise RuntimeError("ffmpeg did not provide stdout.")

    segments: List[Dict[str, object]] = []
    chunk_index = 0
    overlap_tail = b""
    last_norm = ""
    last_end = 0.0

    while True:
        chunk = _read_chunk(process.stdout, chunk_bytes)
        if not chunk:
            break

        payload = overlap_tail + chunk if overlap_tail else chunk
        overlap_used = overlap_seconds if overlap_tail else 0
        offset = max(chunk_index * chunk_seconds - overlap_used, 0)

        result = transcribe_pcm(
            endpoint,
            payload,
            sample_rate,
            language=language,
            api_key=api_key,
            timeout=timeout,
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

        overlap_tail = chunk[-overlap_bytes:] if overlap_bytes and len(chunk) >= overlap_bytes else chunk
        chunk_index += 1

    process.communicate()
    if process.returncode not in (0, None):
        raise RuntimeError("ffmpeg failed during transcription.")
    return segments


def _generate_from_existing(
    existing: Dict[str, object],
    source_lang: str,
    target_lang: str,
    media_path: Path,
    output_path_for,
    require_translate: bool = False,
) -> Dict[str, object]:
    temp_path = None
    try:
        if existing["kind"] == "embedded":
            temp_handle = tempfile.NamedTemporaryFile(delete=False, suffix=".srt")
            temp_handle.close()
            temp_path = Path(temp_handle.name)
            extract_embedded_sub(media_path, int(existing["stream_index"]), temp_path)
            source_path = temp_path
        else:
            source_path = Path(existing["path"])

        segments = parse_srt(source_path.read_text(encoding="utf-8", errors="ignore"))
        if not segments:
            return {"error": "Existing subtitle was empty."}

        outputs: List[str] = []
        if target_lang == source_lang and not require_translate:
            output = output_path_for(source_lang)
            output.write_text(format_srt(segments), encoding="utf-8")
            outputs.append(str(output))
            return {"outputs": outputs}

        translated = translate_segments(
            segments,
            target_lang,
            api_key=_google_api_key(),
            source_language=source_lang if source_lang != "und" else None,
        )
        output = output_path_for(target_lang)
        output.write_text(format_srt(translated), encoding="utf-8")
        outputs.append(str(output))
        return {"outputs": outputs}
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def _resolve_existing_sub(media: Dict[str, object], existing_id: Optional[str]) -> Optional[Dict[str, object]]:
    subs = list(media.get("sidecar_subs", [])) + list(media.get("embedded_subs", []))
    if not subs:
        return None
    if existing_id:
        for item in subs:
            if item.get("id") == existing_id:
                return item
    return subs[0]


def _google_api_key() -> str:
    key = os.getenv("GOOGLE_TRANSLATE_API_KEY")
    if not key:
        raise ValueError("Google Translate API key is required for translation.")
    return key


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Subgen web UI server.")
    parser.add_argument("--media-dir", default=None, help="Base media directory to scan.")
    parser.add_argument("--endpoint", default=None, help="STT server endpoint.")
    parser.add_argument("--host", default="0.0.0.0", help="Listen host.")
    parser.add_argument("--port", type=int, default=8080, help="Listen port.")
    args = parser.parse_args()

    config = load_config()
    media_dir = args.media_dir or config.get("media_dir") or "/agent/workspace/media_test"
    endpoint = args.endpoint or config.get("stt_endpoint") or "https://stt.rtek.dev"
    print(f"[subgen] using config_path={CONFIG_PATH}")
    print(f"[subgen] config values: media_dir={media_dir} stt_endpoint={endpoint}")
    app = create_app(str(media_dir), str(endpoint))
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
