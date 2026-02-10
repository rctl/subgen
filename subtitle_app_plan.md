# Local Subtitle Generator Plan (Python)

## Goal
Build a local program that takes a video file in common Jellyfin-compatible formats (e.g., MKV, MP4, AVI), extracts audio, transcribes speech to English with timestamps, and outputs a subtitle file (SRT/VTT).

## Scope
- Input: Single video file path
- Output: Subtitle file with timings and text
- Local execution, no cloud dependency required
- Primary languages: Swedish and Chinese, with configurable language selection

## Proposed Stack
- Language: Python
- Media handling: FFmpeg (CLI) for audio extraction
- HTTP client: `httpx` or `requests`
- Transcription: Remote speech-to-text service compatible with Jarvis `stt-server`
- Subtitle output: SRT and VTT writers

## Workflow
1. Validate input video file
2. Extract audio to 16 kHz mono PCM (int16 LE)
3. POST raw PCM bytes to `/transcribe` with `X-Sample-Rate: 16000` and target language
4. Build subtitle segments
5. Write `.srt` and optionally `.vtt`
6. Clean up temp artifacts

## Components
- `subgen/__main__.py`: CLI entry point
- `subgen/media.py`: FFmpeg wrapper (audio extraction)
- `subgen/transcribe.py`: Jarvis `stt-server` API client and timestamp parsing
- `subgen/subtitles.py`: SRT/VTT generation
- `subgen/config.py`: CLI flags and defaults

## CLI Design
- `python -m subgen --input /path/video.mkv --output /path/video.srt`
- Optional flags:
  - `--model` remote model name or ID (if supported by server)
  - `--endpoint` remote API base URL (expects `/transcribe`)
  - `--api-key` API token
  - `--lang` default `sv` (allow `zh`, `zh-CN`, `zh-TW`)
  - `--format` `srt|vtt`
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

## Jarvis STT Server Compatibility
- Endpoint: `POST /transcribe`
- Request body: raw PCM int16 LE audio (mono)
- Header: `X-Sample-Rate` (e.g., `16000`)
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

## Open Questions
- Confirm the exact `stt-server` response schema and whether it includes word-level timestamps
- Do we need diarization (speaker labels)?
- Should we support auto-translation in a follow-up app?
