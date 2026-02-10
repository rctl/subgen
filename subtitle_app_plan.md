# Local Subtitle Generator Plan (Python)

## Goal
Build a local program that takes a video file in common Jellyfin-compatible formats (e.g., MKV, MP4, AVI), extracts audio, transcribes English speech with timestamps, and outputs an SRT subtitle file.

## Scope
- Input: Single video file path
- Output: SRT subtitle file with timings and text
- Local pipeline with required local STT microservice
- V1: English-only transcription, no translation

## Proposed Stack
- Language: Python
- Media handling: FFmpeg (CLI) for audio extraction
- HTTP client: `httpx` or `requests`
- Transcription: Jarvis `stt-server` microservice only (no local Whisper support in subgen)
- Subtitle output: SRT writer

## Workflow
1. Validate input video file
2. Extract audio to 16 kHz mono PCM (int16 LE)
3. POST raw PCM bytes to `/transcribe` with `X-Sample-Rate: 16000` (V1: English)
4. Build subtitle segments
5. Write `.srt`
6. Clean up temp artifacts

## Components
- `subgen/__main__.py`: CLI entry point
- `subgen/media.py`: FFmpeg wrapper (audio extraction)
- `subgen/transcribe.py`: Jarvis `stt-server` API client and timestamp parsing
- `subgen/subtitles.py`: SRT generation
- `subgen/config.py`: CLI flags and defaults

## CLI Design
- `python -m subgen --input /path/video.mkv --output /path/video.srt`
- Optional flags:
  - `--model` remote model name or ID (if supported by server)
  - `--endpoint` remote API base URL (expects `/transcribe`)
  - `--api-key` API token
  - `--lang` fixed `en` in V1
  - `--format` `srt`
  - `--threads`

## Transcription Strategy
- Use Whisper JSON response `segments` for timings
- Chunk audio for long videos to reduce memory usage
- Use fixed-length chunks with overlap to preserve context
- Merge short segments for readability and de-duplicate overlaps

## Timing Synchronization
- Decode audio via FFmpeg using timestamps (do not time-stretch)
- For each chunk, track its absolute start time in the source audio
- Add chunk start offset to each `segments[].start`/`segments[].end`
- Overlap chunks (e.g., 3–5 seconds) to preserve context, then drop duplicated text
- Keep original timebase; never re-sample to change playback speed

## De-duplication Rule
- Normalize text (lowercase, trim, collapse whitespace)
- If a segment starts within the overlap window and its normalized text matches the tail
  of the previous chunk, drop it

## Jarvis STT Server Compatibility
- Endpoint: `POST /transcribe`
- Request body: raw PCM int16 LE audio (mono)
- Header: `X-Sample-Rate` (e.g., `16000`)
- Language marker: `X-Lang: en` header (preferred) or `?lang=en` query param
- Response: Whisper `transcribe` JSON with top-level `text`, `language`, and `segments`
- Timing source: `segments[].start`, `segments[].end`, `segments[].text`

## Error Handling
- Validate FFmpeg availability
- Fail fast on missing files or unsupported formats
- Log and exit on transcription errors
- Retry transient API failures with backoff

## Performance Considerations
- Stream extraction and transcription in chunks
- Cache extracted audio if re-running
- Allow setting CPU threads where applicable
- Use backpressure and bounded queues to cap memory for multi-hour videos
- Write intermediate segment results to disk to avoid holding all segments in RAM
- Support resume by persisting chunk offsets and completed segments under `/tmp` (crash-safe only)

## Resume State Layout
- `/tmp/subgen/<job_id>/manifest.json` with input path, chunk size, overlap, and last completed chunk
- `/tmp/subgen/<job_id>/segments.jsonl` append-only segment records

## Testing Plan
- Unit tests for subtitle formatting
- Integration test with a short sample video
- Golden file comparison for SRT output

## Milestones
1. CLI skeleton and argument parsing
2. FFmpeg audio extraction
3. Whisper transcription integration
4. Subtitle generation
5. End-to-end test
6. Packaging and documentation

## Future (V2)
- Translation via separate HTTP microservice (`POST /translate`) with `{source_lang, target_lang, segments[]}`
- Additional input languages beyond English
- Diarization (speaker labels)
