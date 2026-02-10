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
