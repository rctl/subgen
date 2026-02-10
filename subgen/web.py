from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import time
import uuid
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


CONFIG_PATH = os.environ.get("SUBGEN_CONFIG_PATH", "/app/config.json")
FALLBACK_CONFIG_PATH = os.path.join(os.getcwd(), "config.json")


def load_config() -> Dict[str, object]:
    for path in (CONFIG_PATH, FALLBACK_CONFIG_PATH):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            print(f"[subgen] loaded config from {path}")
            return payload
        except FileNotFoundError:
            print(f"[subgen] config not found at {path}")
            continue
        except json.JSONDecodeError as exc:
            print(f"[subgen] config parse error at {path}: {exc}")
            return {}
    return {}


def create_app(base_dir: str, stt_endpoint: str) -> Flask:
    app = Flask(__name__, static_folder="web/static")
    app.config["BASE_DIR"] = base_dir
    app.config["STT_ENDPOINT"] = stt_endpoint
    app.config["MEDIA_CACHE"] = []
    app.config["SCAN_LOCK"] = threading.Lock()
    app.config["JOB_LOCK"] = threading.Lock()
    app.config["JOBS"] = {}
    app.config["SCAN_STATE"] = {
        "running": False,
        "total_files": 0,
        "scanned_files": 0,
        "scanned_videos": 0,
        "current_file": "",
        "error": "",
        "last_completed_at": None,
    }
    print(f"[subgen] config base_dir={base_dir} stt_endpoint={stt_endpoint}")
    _start_scan(app, force=True)

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(app.static_folder, filename)

    @app.route("/api/media")
    def api_media():
        rescan = request.args.get("rescan") == "1"
        if rescan:
            _start_scan(app, force=True)
        items = app.config.get("MEDIA_CACHE", [])
        return jsonify(
            {
                "media_dir": app.config["BASE_DIR"],
                "items": items,
                "scan": dict(app.config["SCAN_STATE"]),
            }
        )

    @app.route("/api/scan-status")
    def api_scan_status():
        return jsonify(dict(app.config["SCAN_STATE"]))

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
        job_id = _create_job(app, data)
        return jsonify({"job_id": job_id})

    @app.route("/api/jobs/<job_id>")
    def api_job_status(job_id: str):
        job = app.config["JOBS"].get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)

    @app.route("/api/jobs")
    def api_jobs_list():
        jobs = list(app.config["JOBS"].values())
        jobs.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return jsonify({"jobs": jobs})

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


def _create_job(app: Flask, payload: Dict[str, object]) -> str:
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "status": "queued",
        "stage": "queued",
        "message": "",
        "outputs": [],
        "error": "",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    with app.config["JOB_LOCK"]:
        app.config["JOBS"][job_id] = job
    thread = threading.Thread(target=_run_job, args=(app, job_id, payload), daemon=True)
    thread.start()
    return job_id


def _update_job(app: Flask, job_id: str, **kwargs: object) -> None:
    with app.config["JOB_LOCK"]:
        job = app.config["JOBS"].get(job_id)
        if not job:
            return
        job.update(kwargs)
        job["updated_at"] = int(time.time())


def _run_job(app: Flask, job_id: str, payload: Dict[str, object]) -> None:
    try:
        _update_job(app, job_id, status="running", stage="init", message="Preparing job")
        result = _generate_outputs(app, payload, job_id)
        _update_job(
            app,
            job_id,
            status="completed",
            stage="done",
            message="Completed",
            outputs=result.get("outputs", []),
        )
    except Exception as exc:
        _update_job(app, job_id, status="failed", stage="error", error=str(exc))


def _generate_outputs(app: Flask, payload: Dict[str, object], job_id: str) -> Dict[str, object]:
    media_path = payload.get("media_path")
    source_lang = (payload.get("source_lang") or "").strip() or "und"
    target_lang = (payload.get("target_lang") or "").strip() or source_lang
    mode = (payload.get("mode") or "use_existing").strip()
    existing_id = payload.get("existing_sub_id")

    if not media_path:
        raise ValueError("Missing media_path")

    media = describe_media(Path(media_path))
    output_dir = Path(media_path).parent
    stem = Path(media_path).stem

    def output_path_for(lang: str) -> Path:
        return output_dir / f"{stem}.gen_{lang}.srt"

    existing = _resolve_existing_sub(media, existing_id)

    if mode == "translate_existing":
        if not existing:
            raise ValueError("No existing subtitle available.")
        _update_job(app, job_id, stage="translate", message="Translating existing subtitles")
        return _generate_from_existing(
            existing,
            source_lang,
            target_lang,
            Path(media_path),
            output_path_for,
            require_translate=True,
        )

    if mode != "transcribe":
        raise ValueError(f"Unknown mode: {mode}")

    _update_job(app, job_id, stage="transcribe", message="Transcribing audio")
    segments = transcribe_media(
        Path(media_path),
        app.config["STT_ENDPOINT"],
        source_lang,
    )
    source_output = output_path_for(source_lang)
    source_output.write_text(format_srt(segments), encoding="utf-8")

    outputs = [str(source_output)]
    if target_lang != source_lang:
        _update_job(app, job_id, stage="translate", message="Translating subtitles")
        translated = translate_segments(
            segments,
            target_lang,
            api_key=_google_api_key(),
            source_language=source_lang if source_lang != "und" else None,
        )
        target_output = output_path_for(target_lang)
        target_output.write_text(format_srt(translated), encoding="utf-8")
        outputs.append(str(target_output))

    return {"outputs": outputs}


def _start_scan(app: Flask, force: bool = False) -> None:
    with app.config["SCAN_LOCK"]:
        state = app.config["SCAN_STATE"]
        if state["running"] and not force:
            return
        if state["running"] and force:
            return
        state["running"] = True
        state["total_files"] = 0
        state["scanned_files"] = 0
        state["scanned_videos"] = 0
        state["current_file"] = ""
        state["error"] = ""
        thread = threading.Thread(target=_scan_worker, args=(app,), daemon=True)
        thread.start()


def _scan_worker(app: Flask) -> None:
    print("[subgen] scanning media library...")

    def on_progress(progress: Dict[str, object]) -> None:
        state = app.config["SCAN_STATE"]
        state["total_files"] = int(progress.get("total_files", 0))
        state["scanned_files"] = int(progress.get("scanned_files", 0))
        state["scanned_videos"] = int(progress.get("scanned_videos", 0))
        state["current_file"] = str(progress.get("current_file", ""))

    try:
        items = scan_media(app.config["BASE_DIR"], progress_callback=on_progress)
        app.config["MEDIA_CACHE"] = items
        app.config["SCAN_STATE"]["last_completed_at"] = int(time.time())
        print(f"[subgen] scan complete: {len(items)} items")
    except Exception as exc:
        app.config["SCAN_STATE"]["error"] = str(exc)
        print(f"[subgen] scan failed: {exc}")
    finally:
        app.config["SCAN_STATE"]["running"] = False
        app.config["SCAN_STATE"]["current_file"] = ""


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
    google_key = config.get("google_translate_api_key")
    if google_key and not os.environ.get("GOOGLE_TRANSLATE_API_KEY"):
        os.environ["GOOGLE_TRANSLATE_API_KEY"] = str(google_key)
    print(f"[subgen] using config_path={CONFIG_PATH}")
    print(f"[subgen] fallback config_path={FALLBACK_CONFIG_PATH}")
    print(f"[subgen] config values: media_dir={media_dir} stt_endpoint={endpoint}")
    app = create_app(str(media_dir), str(endpoint))
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
