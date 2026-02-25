# files/

Shared transport-agnostic file helpers used by Telegram and direct API paths.

## Files

- `allowed_roots.py`: `resolve_allowed_roots(file_access, workspace)`
- `tags.py`: file-tag parsing, MIME detection, media classification
- `storage.py`: filename sanitization and destination-path generation
- `prompt.py`: shared incoming-file prompt builder (`MediaInfo`, `build_media_prompt`)

## Why this module exists

Previously, Telegram-specific modules held duplicated file logic. `files/` centralizes that logic so both transports use the same behavior for:

- `<file:...>` tag parsing
- MIME detection and image/document decisions
- upload/download path handling
- incoming file prompt text construction

## Public API

### `allowed_roots.resolve_allowed_roots(...)`

Maps config `file_access` to allowed path roots:

- `all` -> `None` (unrestricted)
- `home` -> `[Path.home()]`
- `workspace` -> `[workspace]`
- unknown value -> `None`

Used by Telegram send path and API `GET /files` path checks.

### `storage.sanitize_filename(name)`

- removes `/`, `\`, and null bytes
- collapses repeated underscores
- strips edge punctuation/space
- truncates to 120 chars
- fallback `"file"` when empty

### `storage.prepare_destination(base_dir, file_name)`

- uses UTC date folder: `base_dir/YYYY-MM-DD/`
- creates directories as needed
- appends `_1`, `_2`, ... to avoid collisions

### `tags`

- `FILE_PATH_RE`: regex for `<file:...>` tags
- `extract_file_paths(text)`
- `strip_file_tags(text)`
- `guess_mime(path)`:
  - magic-bytes detection via `filetype`
  - fallback to `mimetypes` extension detection
  - default `application/octet-stream`
- `classify_mime(mime)` -> `photo` / `audio` / `video` / `document`
- `is_image_path(path_str)`:
  - extension-based image check
  - excludes SVG/SVGZ

Dependency note:

- `guess_mime(...)` relies on runtime dependency `filetype>=1.2.0`.

### `prompt`

`MediaInfo` dataclass fields:

- `path`
- `media_type`
- `file_name`
- `caption`
- `original_type`

`build_media_prompt(info, workspace, transport="")`:

- emits `[INCOMING FILE]` block used as agent input
- path is made workspace-relative when possible
- optional transport label (`via Telegram`, `via API`)
- adds transcribe/process hints for audio/video media types
- appends caption as `User message: ...` when present

## Integration points in codebase

- `ductor_bot/bot/media.py`:
  - `MediaInfo`
  - `build_media_prompt(..., transport="Telegram")`
  - `prepare_destination`, `sanitize_filename`, `guess_mime`
- `ductor_bot/bot/sender.py`:
  - `FILE_PATH_RE`, `extract_file_paths`, `guess_mime`
- `ductor_bot/bot/app.py`:
  - `resolve_allowed_roots` for outbound file access policy
- `ductor_bot/orchestrator/core.py`:
  - `resolve_allowed_roots` for API file endpoint policy
- `ductor_bot/api/server.py`:
  - all file helpers for upload/download/file-ref extraction/prompt creation

## Behavioral notes

- Date folders are UTC-based (`YYYY-MM-DD`).
- API uploads land in `~/.ductor/workspace/api_files/YYYY-MM-DD/`.
- Telegram media downloads land in `~/.ductor/workspace/telegram_files/YYYY-MM-DD/`.
