# Web UI Plan

## Goals
- Provide a dark mode web UI to browse a media folder and manage subtitles.
- List videos with title, path, and subtitle availability (embedded + sidecar files).
- Allow search across large libraries (hundreds of titles).
- Enable subtitle generation or translation with clear language selection.
- Output generated subtitles next to the media file with `gen_[langcode].srt` naming so Jellyfin can pick them up.

## Assumptions
- Backend lives in this repo and can access the media path on disk.
- STT uses the existing remote Whisper-compatible API.
- Translation uses Google Translate API.

## UX Flow
1. User selects or configures a base media folder (e.g., `/path/to/media`).
2. UI scans for video files and shows a list with:
   - Title (from filename, optional metadata)
   - Path
   - Subtitle status
   - Action button: `Generate Subtitle`
3. Search bar filters the list by title.
4. Clicking `Generate Subtitle` opens a modal:
   - Source audio language (default detect if available, else required)
   - Target language (default same as source for no translation)
   - Source subtitle option:
     - Default: use existing subtitle if found
     - Options: `Use existing`, `Translate from existing`, `Transcribe` (force)
5. Subtitles are written next to the file as `gen_[langcode].srt`.

## Subtitle Discovery Rules
- Embedded subtitles: detect via `ffprobe` and list language/title.
- Sidecar subtitles: scan same directory for extensions `.srt`, `.vtt`, `.ass`, `.ssa`, `.sub`.
- Normalize language codes from filenames and metadata.
- Display counts and language labels in the UI.

## Backend Components
- `GET /api/media?path=/path/to/media`
  - Scans for video files and returns metadata + subtitle inventory.
- `GET /api/media/:id`
  - Returns detailed subtitle list and media metadata.
- `POST /api/subtitles/generate`
  - Body: `{media_path, source_lang, target_lang, mode}`
  - Modes: `use_existing`, `translate_existing`, `transcribe`
  - Writes output `gen_[langcode].srt` next to media.
- `GET /api/jobs/:id`
  - Progress for long running tasks.

## Data Model
- Media item:
  - `id`, `path`, `title`, `duration`
  - `embedded_subs[]` with `lang`, `title`
  - `sidecar_subs[]` with `lang`, `path`, `format`
  - `has_subs` boolean

## UI Layout
- Dark mode theme with:
  - Header: folder selector and search bar
  - Main list: virtualized table for performance
  - Row actions: subtitle status + `Generate Subtitle`
  - Modal for language selection and mode
- Provide status badges: `Embedded`, `Sidecar`, `Generated`.

## Language Handling
- Default source language:
  - Use embedded metadata if audio language exists.
  - Otherwise prompt user selection.
- Target language:
  - Default to same as source for no translation.
  - If different, run translation after STT or from existing subtitles.

## Implementation Steps
1. Add backend media scanning and subtitle discovery utilities.
2. Add API endpoints for list and generate.
3. Implement job tracking for STT and translation.
4. Build dark mode UI with virtualized list and modal.
5. Add configuration for base path and API keys.
6. Add logging and basic error handling.

## Open Questions
- How to detect audio language from metadata reliably.
- Whether to allow multiple base folders.
- Where to store job history and logs.
