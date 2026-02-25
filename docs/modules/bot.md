# bot/

Telegram interface layer (`aiogram`): handlers, middleware, streaming delivery, callbacks, and rich sender.

## Files

- `app.py`: `TelegramBot` lifecycle, handler registration, callback routing, observer bridges
- `message_dispatch.py`: shared streaming/non-streaming execution paths
- `handlers.py`: command helper handlers (`/new`, `/stop`, generic command path)
- `response_format.py`: shared command/error text builders (`/new`, `/stop`, session error hints)
- `middleware.py`: `AuthMiddleware`, `SequentialMiddleware`, quick-command bypass, queue tracking
- `welcome.py`: `/start` text + quick action callbacks (`w:*`)
- `file_browser.py`: interactive `~/.ductor/` browser (`sf:`/`sf!`)
- `streaming.py`, `edit_streaming.py`: stream editors
- `sender.py`: rich text/file sending (`send_rich`, `<file:...>` handling, MIME-based photo/document choice)
- `formatting.py`: markdown-to-Telegram HTML conversion/chunking
- `buttons.py`: `[button:...]` parsing
- `media.py`: media download/index/prompt conversion (delegates shared helpers in `ductor_bot/files/`)
- `abort.py`, `dedup.py`, `typing.py`, `topic.py`: shared runtime helpers

## Command ownership

Bot-level handlers (`app.py`):

- `/start`, `/help`, `/info`, `/showfiles`, `/stop`, `/restart`, `/new`

Orchestrator-routed commands:

- `/status`, `/memory`, `/model`, `/cron`, `/diagnose`, `/upgrade`

## Middleware behavior

### `AuthMiddleware`

- drops message/callback updates from users outside `allowed_user_ids`

### `SequentialMiddleware`

Message flow order:

1. abort trigger check (`/stop` and bare abort words) before lock
2. quick command bypass (`/status`, `/memory`, `/cron`, `/diagnose`, `/model`, `/showfiles`)
3. dedupe by `chat_id:message_id`
4. acquire per-chat lock for normal messages
5. queued messages get indicator + cancel button (`mq:<entry_id>`)

`/model` special case in quick-command handler: when chat is busy (active process or queued messages), bot returns immediate \"agent is working\" text instead of opening the selector.

Queue API:

- `is_busy(chat_id)`
- `has_pending(chat_id)`
- `cancel_entry(chat_id, entry_id)`
- `drain_pending(chat_id)`

## Message dispatch (`message_dispatch.py`)

### Non-streaming

`run_non_streaming_message()`:

- `TypingContext`
- `orchestrator.handle_message()`
- `send_rich()`

### Streaming

`run_streaming_message()`:

- create stream editor
- use `StreamCoalescer` for text batching
- forward callbacks:
  - text delta
  - tool activity
  - system status (`thinking`, `compacting`, `recovering`)
- finalize editor
- fallback path:
  - `stream_fallback` or empty stream -> `send_rich(full_text)`
  - otherwise only send extracted files via `send_files_from_text()`

## Callback routing

Handled namespaces in `TelegramBot._route_special_callback`:

- `mq:*` queue cancel
- `upg:*` upgrade callbacks
- `ms:*` model selector
- `crn:*` cron selector
- `sf:*` / `sf!` file browser

Lock behavior:

- model selector, cron selector, and `sf!` file-request callbacks acquire per-chat lock
- queue cancel, upgrade callbacks, and `sf:` directory navigation do not

Generic callbacks are converted to user answer text and routed through normal message flow.

## Forum topic support

All send paths propagate `message_thread_id` from topic messages via `get_thread_id()`.

Sessions remain keyed by `chat_id` (no per-topic session split).

## File safety and `file_access`

`send_file()` validates paths against allowed roots.

`file_access` mapping:

- `all` -> unrestricted
- `home` -> only under home directory
- `workspace` -> only under `~/.ductor/workspace`

Implementation note:

- allowed roots are resolved through `files.allowed_roots.resolve_allowed_roots(...)` (shared with API server).
- MIME detection for send path uses `files.tags.guess_mime(...)` (magic bytes + extension fallback), and SVG is sent as document.

## Observer bridges in bot layer

`TelegramBot._on_startup()` wires:

- cron result handler
- heartbeat result handler
- webhook result handler
- webhook wake handler

Wake handler path (`_handle_webhook_wake`) acquires per-chat lock, routes prompt through orchestrator, then sends response.

Webhook result forwarding sends only `cron_task` results because wake responses are sent directly by wake handler.
