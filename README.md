# subgen

Generate SRT subtitles from video files using the Jarvis `stt-server`.

## Requirements
- Python 3.10+
- `ffmpeg` in PATH

## Install
```bash
pip install -r requirements.txt
```

## Usage
```bash
python -m subgen \
  --input /agent/workspace/jellyfin_sample.mkv \
  --output /agent/workspace/jellyfin_sample.srt \
  --endpoint http://localhost:8000 \
  --lang en \
  --translate-to sv
```

Translation notes:
- Set `GOOGLE_TRANSLATE_API_KEY` or pass `--google-api-key`.
- Translated output is written to `output.{lang}.srt` (e.g., `MovieName.sv.srt`).
- If the original `output.srt` already exists, STT is skipped unless `--force-stt` is used.
- For Jellyfin to detect subtitles, keep the subtitle file next to the video and include the movie filename (e.g., `MovieName.gen_en.srt`).
- Transcription now uses VAD-gated sub-segments (threshold `0.30`) to skip obvious non-speech audio.
- VAD debug logs are printed to stdout per chunk with max score and kept regions.

## Web UI
Run the web UI server:
```bash
python -m subgen.web --media-dir /path/to/media --endpoint https://stt.rtek.dev
```
Open `http://localhost:8080` to browse media, inspect subtitles, and generate `gen_[lang].srt` files.

The web UI reads `media_dir` and `stt_endpoint` from `config.json` if CLI flags are not provided.
It also supports optional `index_path` in `config.json` to override where the media index is stored.
Relative `index_path` values are resolved under `media_dir`; absolute paths can place the index outside `media_dir`.
`vad_threshold` is optional in `config.json` and defaults to `0.30`.
Translation provider selection is available in the UI with `Google Translate` and `Anthropic`.
`Google Translate` remains unchanged and uses `google_translate_api_key`.
For Anthropic, set `anthropic_api_key` and `anthropic_model` in `config.json`.
Anthropic model is passed through directly to the SDK exactly as configured.
