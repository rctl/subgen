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
  --endpoint http://localhost:8000
```
