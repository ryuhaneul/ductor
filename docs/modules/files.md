# files/

Shared transport-agnostic file helpers used by Telegram, Matrix, and direct API paths.

## Files

- `files/allowed_roots.py`: `resolve_allowed_roots(file_access, workspace)`
- `files/tags.py`: file-tag parsing, MIME detection, media classification
- `files/storage.py`: filename sanitization + destination generation
- `files/prompt.py`: incoming-file prompt builder (`MediaInfo`, `build_media_prompt`)

## Purpose

Centralize file logic so Telegram, Matrix, and API use identical behavior for:

- `<file:...>` parsing
- MIME/type detection
- safe upload/download path handling
- incoming media prompt construction

## Core helpers

### `resolve_allowed_roots(...)`

Maps `file_access` to allowed roots:

- `all` -> unrestricted (`None`)
- `home` -> `[Path.home()]`
- `workspace` -> `[workspace]`
- unknown -> `[workspace]` fallback (restrictive)

### `sanitize_filename(name)`

- strips separators/unsafe chars
- normalizes repeated separators
- truncates long names
- fallback `"file"`

### `prepare_destination(base_dir, file_name)`

- uses date folder `YYYY-MM-DD`
- creates directories as needed
- de-duplicates via `_1`, `_2`, ... suffix

### `tags` helpers

- parse `<file:...>` tags and file URIs
- normalize Windows path variants
- detect MIME via `filetype` with extension fallback
- classify media as `photo|audio|video|document`

### `build_media_prompt(info, workspace, transport=...)`

Builds standardized `[INCOMING FILE]` prompt blocks for agent input.

## Integration points

- Telegram media ingest/send: `messenger/telegram/media.py`, `messenger/telegram/sender.py`, `messenger/telegram/app.py`
- Matrix media ingest: `messenger/matrix/media.py`, `messenger/matrix/bot.py`
- API upload/download and file-ref extraction: `api/server.py`
- API startup file-context wiring: `orchestrator/lifecycle.py`

## Runtime paths

- Telegram uploads: `~/.ductor/workspace/telegram_files/YYYY-MM-DD/`
- Matrix uploads: `~/.ductor/workspace/matrix_files/YYYY-MM-DD/`
- API uploads: `~/.ductor/workspace/api_files/YYYY-MM-DD/`
