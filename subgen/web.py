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
    INDEX_FILENAME,
    describe_media,
    extract_embedded_sub,
    load_media_index,
    save_media_index,
    scan_media_with_index,
)
from .subtitles import format_srt, parse_srt
from .transcribe import transcribe_media
from .translate import translate_segments, translate_segments_anthropic

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


def create_app(
    base_dir: str,
    stt_endpoint: str,
    index_path: Optional[str] = None,
    vad_threshold: float = 0.30,
) -> Flask:
    app = Flask(__name__, static_folder="web/static")
    app.config["BASE_DIR"] = base_dir
    app.config["STT_ENDPOINT"] = stt_endpoint
    app.config["VAD_THRESHOLD"] = vad_threshold
    app.config["INDEX_PATH"] = index_path
    app.config["MEDIA_CACHE"] = load_media_index(base_dir, index_path=index_path)
    app.config["JOB_LOCK"] = threading.Lock()
    app.config["JOBS"] = {}
    if app.config["MEDIA_CACHE"]:
        print(f"[subgen] loaded {len(app.config['MEDIA_CACHE'])} media items from subgen.json")
    else:
        print("[subgen] no cached media index loaded")
    print(
        f"[subgen] config base_dir={base_dir} stt_endpoint={stt_endpoint} vad_threshold={vad_threshold:.2f}"
    )
    startup_index_path = _startup_index_path(base_dir, index_path)
    startup_has_index = startup_index_path.exists() and startup_index_path.is_file()
    startup_full_scan = not startup_has_index
    print(
        f"[subgen] startup index_path={startup_index_path} exists={startup_has_index}; "
        f"startup_scan={'full' if startup_full_scan else 'delta'}"
    )
    _start_scan(app, full_scan=startup_full_scan)

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
            _start_scan(app, full_scan=False)
        items = app.config.get("MEDIA_CACHE", [])
        return jsonify({"media_dir": app.config["BASE_DIR"], "items": items})

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

    @app.route("/api/jobs")
    def api_jobs_list():
        jobs = list(app.config["JOBS"].values())
        jobs.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return jsonify({"jobs": jobs})

    @app.route("/api/jobs/<job_id>", methods=["DELETE"])
    def api_job_delete(job_id: str):
        with app.config["JOB_LOCK"]:
            job = app.config["JOBS"].get(job_id)
            if not job:
                return jsonify({"removed": False, "canceled": False})
            if job.get("status") in {"queued", "running"}:
                job["cancel_requested"] = True
                job["status"] = "cancelling"
                job["message"] = "Cancellation requested"
                job["updated_at"] = int(time.time())
                return jsonify({"removed": False, "canceled": True})
            app.config["JOBS"].pop(job_id, None)
        return jsonify({"removed": True, "canceled": False})

    return app


def _generate_from_existing(
    existing: Dict[str, object],
    source_lang: str,
    target_lang: str,
    media_path: Path,
    output_path_for,
    require_translate: bool = False,
    translate_provider: str = "google",
    anthropic_model: Optional[str] = None,
    anthropic_max_parallel: int = 5,
    progress_callback=None,
    should_cancel=None,
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
        if should_cancel and should_cancel():
            raise RuntimeError("Job canceled.")

        outputs: List[str] = []
        if target_lang == source_lang and not require_translate:
            output = output_path_for(source_lang)
            output.write_text(format_srt(segments), encoding="utf-8")
            outputs.append(str(output))
            return {"outputs": outputs}

        translated = _translate_with_provider(
            provider=translate_provider,
            segments=segments,
            target_lang=target_lang,
            source_lang=source_lang,
            anthropic_model=anthropic_model,
            anthropic_max_parallel=anthropic_max_parallel,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
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


def _anthropic_api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise ValueError("Anthropic API key is required for Anthropic translation.")
    return key


def _translate_with_provider(
    provider: str,
    segments: List[Dict[str, object]],
    target_lang: str,
    source_lang: str,
    anthropic_model: Optional[str],
    anthropic_max_parallel: int,
    progress_callback=None,
    should_cancel=None,
) -> List[Dict[str, object]]:
    provider_norm = (provider or "google").strip().lower()
    src = source_lang if source_lang != "und" else None
    if provider_norm == "anthropic":
        model = anthropic_model or "claude-3-5-sonnet-latest"
        return translate_segments_anthropic(
            segments,
            target_lang,
            api_key=_anthropic_api_key(),
            model=model,
            max_parallel=max(1, int(anthropic_max_parallel)),
            source_language=src,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
    return translate_segments(
        segments,
        target_lang,
        api_key=_google_api_key(),
        source_language=src,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
    )


def _create_job(app: Flask, payload: Dict[str, object]) -> str:
    media_path = str(payload.get("media_path") or "")
    mode = str(payload.get("mode") or "transcribe")
    title = Path(media_path).stem if media_path else "Subtitle job"
    action = "translate" if mode == "translate_existing" else "transcribe"
    job_name = f"{title} ({action})"
    job_type = "translate" if mode == "translate_existing" else "transcribe"
    job_id = _create_job_record(app, job_type=job_type, name=job_name)
    thread = threading.Thread(target=_run_generate_job, args=(app, job_id, payload), daemon=True)
    thread.start()
    return job_id


def _create_job_record(app: Flask, job_type: str, name: str) -> str:
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "type": job_type,
        "name": name,
        "status": "queued",
        "stage": "queued",
        "message": "Queued",
        "progress_current": 0,
        "progress_total": 0,
        "progress_percent": 0,
        "outputs": [],
        "error": "",
        "cancel_requested": False,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    with app.config["JOB_LOCK"]:
        app.config["JOBS"][job_id] = job
    return job_id


def _update_job(app: Flask, job_id: str, **kwargs: object) -> None:
    with app.config["JOB_LOCK"]:
        job = app.config["JOBS"].get(job_id)
        if not job:
            return
        job.update(kwargs)
        job["updated_at"] = int(time.time())


def _is_cancel_requested(app: Flask, job_id: str) -> bool:
    with app.config["JOB_LOCK"]:
        job = app.config["JOBS"].get(job_id)
        return bool(job and job.get("cancel_requested"))


def _finish_canceled(app: Flask, job_id: str) -> None:
    _update_job(app, job_id, status="canceled", stage="canceled", message="Canceled")


def _run_generate_job(app: Flask, job_id: str, payload: Dict[str, object]) -> None:
    try:
        _update_job(app, job_id, status="running", stage="init", message="Preparing job")
        if _is_cancel_requested(app, job_id):
            _finish_canceled(app, job_id)
            return
        result = _generate_outputs(app, payload, job_id)
        if _is_cancel_requested(app, job_id):
            _finish_canceled(app, job_id)
            return
        _update_job(
            app,
            job_id,
            status="completed",
            stage="done",
            message="Completed",
            outputs=result.get("outputs", []),
            progress_current=100,
            progress_total=100,
            progress_percent=100,
        )
    except Exception as exc:
        if str(exc) == "Job canceled.":
            _finish_canceled(app, job_id)
            return
        _update_job(app, job_id, status="failed", stage="error", error=str(exc), message="Failed")


def _generate_outputs(app: Flask, payload: Dict[str, object], job_id: str) -> Dict[str, object]:
    media_path = payload.get("media_path")
    source_lang = (payload.get("source_lang") or "").strip() or "und"
    target_lang = (payload.get("target_lang") or "").strip() or source_lang
    mode = (payload.get("mode") or "use_existing").strip()
    existing_id = payload.get("existing_sub_id")
    translate_provider = (payload.get("translate_provider") or app.config.get("TRANSLATE_PROVIDER_DEFAULT") or "google").strip()
    anthropic_model = app.config.get("ANTHROPIC_MODEL")
    anthropic_max_parallel = int(app.config.get("ANTHROPIC_MAX_PARALLEL", 5))

    if not media_path:
        raise ValueError("Missing media_path")

    media = describe_media(Path(media_path))
    output_dir = Path(media_path).parent
    stem = Path(media_path).stem

    def output_path_for(lang: str) -> Path:
        return output_dir / f"{stem}.gen_{lang}.srt"

    existing = _resolve_existing_sub(media, existing_id)
    should_cancel = lambda: _is_cancel_requested(app, job_id)

    if mode == "translate_existing":
        if not existing:
            raise ValueError("No existing subtitle available.")
        _update_job(app, job_id, stage="translate", message=f"Translating existing subtitles ({translate_provider})")
        return _generate_from_existing(
            existing,
            source_lang,
            target_lang,
            Path(media_path),
            output_path_for,
            require_translate=True,
            translate_provider=translate_provider,
            anthropic_model=anthropic_model,
            anthropic_max_parallel=anthropic_max_parallel,
            progress_callback=lambda progress: _update_job(
                app,
                job_id,
                stage=str(progress.get("stage", "translate")),
                message="Translating existing subtitles",
                progress_current=int(progress.get("processed_segments", 0)),
                progress_total=int(progress.get("total_segments", 0)),
                progress_percent=_percent(
                    int(progress.get("processed_segments", 0)),
                    int(progress.get("total_segments", 0)),
                ),
            ),
            should_cancel=should_cancel,
        )

    if mode != "transcribe":
        raise ValueError(f"Unknown mode: {mode}")

    _update_job(app, job_id, stage="transcribe", message="Transcribing audio")
    segments = transcribe_media(
        Path(media_path),
        app.config["STT_ENDPOINT"],
        source_lang,
        vad_threshold=float(app.config.get("VAD_THRESHOLD", 0.30)),
        progress_callback=lambda progress: _update_job(
            app,
            job_id,
            stage=str(progress.get("stage", "transcribe")),
            message=(
                f"Transcribing chunk {int(progress.get('chunk_index', 0))}"
                + (
                    f"/{int(progress.get('total_chunks', 0))}"
                    if int(progress.get("total_chunks", 0)) > 0
                    else ""
                )
            ),
            progress_current=int(progress.get("chunk_index", 0)),
            progress_total=int(progress.get("total_chunks", 0)),
            progress_percent=int(progress.get("progress_percent", 0)),
        ),
        should_cancel=should_cancel,
    )
    source_output = output_path_for(source_lang)
    source_output.write_text(format_srt(segments), encoding="utf-8")

    outputs = [str(source_output)]
    if target_lang != source_lang:
        _update_job(app, job_id, stage="translate", message=f"Translating subtitles ({translate_provider})")
        translated = _translate_with_provider(
            provider=translate_provider,
            segments=segments,
            target_lang=target_lang,
            source_lang=source_lang,
            anthropic_model=anthropic_model,
            anthropic_max_parallel=anthropic_max_parallel,
            progress_callback=lambda progress: _update_job(
                app,
                job_id,
                stage=str(progress.get("stage", "translate")),
                message="Translating subtitles",
                progress_current=int(progress.get("processed_segments", 0)),
                progress_total=int(progress.get("total_segments", 0)),
                progress_percent=_percent(
                    int(progress.get("processed_segments", 0)),
                    int(progress.get("total_segments", 0)),
                ),
            ),
            should_cancel=should_cancel,
        )
        target_output = output_path_for(target_lang)
        target_output.write_text(format_srt(translated), encoding="utf-8")
        outputs.append(str(target_output))

    return {"outputs": outputs}


def _percent(current: int, total: int) -> int:
    if total <= 0:
        return 0
    return int(max(0, min(100, (current * 100) // total)))


def _startup_index_path(base_dir: str, index_path: Optional[str]) -> Path:
    base_path = Path(base_dir).resolve()
    if index_path and str(index_path).strip():
        candidate = Path(str(index_path).strip())
        if candidate.is_absolute():
            return candidate
        return (base_path / candidate).resolve()
    return base_path / INDEX_FILENAME


def _start_scan(app: Flask, full_scan: bool) -> None:
    with app.config["JOB_LOCK"]:
        for job in app.config["JOBS"].values():
            if job.get("type") == "scan" and job.get("status") in {"queued", "running", "cancelling"}:
                return
    scan_name = "Media library scan (full)" if full_scan else "Media library scan (delta)"
    job_id = _create_job_record(app, job_type="scan", name=scan_name)
    thread = threading.Thread(target=_scan_worker, args=(app, job_id, full_scan), daemon=True)
    thread.start()


def _scan_worker(app: Flask, job_id: str, full_scan: bool) -> None:
    mode = "full" if full_scan else "delta"
    print(f"[subgen] scanning media library ({mode})...")
    _update_job(app, job_id, status="running", stage="scan", message=f"Scanning media files ({mode})")

    def on_progress(progress: Dict[str, object]) -> None:
        current = int(progress.get("scanned_files", 0))
        total = int(progress.get("total_files", 0))
        videos = int(progress.get("scanned_videos", 0))
        current_file = str(progress.get("current_file", ""))
        _update_job(
            app,
            job_id,
            stage="scan",
            message=(
                f"Scanning ({mode}) {current}/{total or '?'} files, videos found: {videos}, "
                f"latest: {Path(current_file).name}"
            ),
            progress_current=current,
            progress_total=total,
            progress_percent=_percent(current, total),
        )

    try:
        items = scan_media_with_index(
            app.config["BASE_DIR"],
            full_scan=full_scan,
            progress_callback=on_progress,
            should_cancel=lambda: _is_cancel_requested(app, job_id),
            seed_items=app.config.get("MEDIA_CACHE", []),
            persist_on_full=False,
            index_path=app.config.get("INDEX_PATH"),
        )
        app.config["MEDIA_CACHE"] = items
        if full_scan:
            save_media_index(
                app.config["BASE_DIR"],
                items,
                async_write=True,
                index_path=app.config.get("INDEX_PATH"),
            )
        print(f"[subgen] scan complete ({mode}): {len(items)} items")
        _update_job(
            app,
            job_id,
            status="completed",
            stage="done",
            message=f"Scan complete ({mode}): {len(items)} videos found",
            progress_current=100,
            progress_total=100,
            progress_percent=100,
        )
    except Exception as exc:
        if str(exc) == "Job canceled.":
            _finish_canceled(app, job_id)
            return
        print(f"[subgen] scan failed: {exc}")
        _update_job(app, job_id, status="failed", stage="error", error=str(exc), message="Scan failed")


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
    index_path = config.get("index_path")
    vad_threshold = float(config.get("vad_threshold") or 0.30)
    translate_provider_default = str(config.get("translate_provider_default") or "google")
    anthropic_model = str(config.get("anthropic_model") or "claude-3-5-sonnet-latest")
    anthropic_max_parallel = int(config.get("anthropic_max_parallel") or 5)
    google_key = config.get("google_translate_api_key")
    anthropic_key = config.get("anthropic_api_key")
    if google_key and not os.environ.get("GOOGLE_TRANSLATE_API_KEY"):
        os.environ["GOOGLE_TRANSLATE_API_KEY"] = str(google_key)
    if anthropic_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = str(anthropic_key)
    print(f"[subgen] using config_path={CONFIG_PATH}")
    print(f"[subgen] fallback config_path={FALLBACK_CONFIG_PATH}")
    print(
        "[subgen] config values: "
        f"media_dir={media_dir} stt_endpoint={endpoint} index_path={index_path or 'media_dir/subgen.json'} "
        f"vad_threshold={vad_threshold:.2f} "
        f"translate_provider_default={translate_provider_default} anthropic_model={anthropic_model} "
        f"anthropic_max_parallel={anthropic_max_parallel}"
    )
    app = create_app(
        str(media_dir),
        str(endpoint),
        str(index_path) if index_path else None,
        vad_threshold=vad_threshold,
    )
    app.config["TRANSLATE_PROVIDER_DEFAULT"] = translate_provider_default
    app.config["ANTHROPIC_MODEL"] = anthropic_model
    app.config["ANTHROPIC_MAX_PARALLEL"] = anthropic_max_parallel
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
