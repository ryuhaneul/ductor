# Architecture

## Runtime Overview

```text
Telegram Update
  -> aiogram Dispatcher/Router
  -> AuthMiddleware (allowlist)
  -> SequentialMiddleware (message updates only)
       - exact /stop or bare abort keyword: kill active CLI process(es) + drain pending queue
       - quick commands (/status /memory /cron /diagnose /model /showfiles): lock bypass
       - otherwise: dedupe + per-chat lock (+ queue tracking)
  -> TelegramBot handler
       - /start /help /info /showfiles /stop /restart /new
       - normal text/media -> Orchestrator
       - callback routes (model selector, cron selector, file browser, upgrade, queue cancel)
  -> Orchestrator
       - slash command -> CommandRegistry
       - directives (@...)
       - normal/streaming flow -> CLIService
  -> CLI provider subprocess (Claude or Codex or Gemini)
  -> Telegram output (stream edits/appends, buttons, files)

Direct API message (optional, `api.enabled=true`)
  -> ApiServer (`/ws`)
  -> per-chat API lock + auth/session routing
  -> Orchestrator.handle_message_streaming(...)
  -> CLI provider subprocess (Claude or Codex or Gemini)
  -> WebSocket stream events + final result
```

Also running in background:

- `CronObserver`: schedules `cron_jobs.json` entries.
- `HeartbeatObserver`: periodic checks in existing sessions.
- `WebhookObserver`: HTTP ingress for external triggers.
- `CleanupObserver`: daily retention cleanup for workspace file directories.
- `GeminiCacheObserver`: periodic Gemini model-cache refresh (`~/.ductor/config/gemini_models.json`).
- `CodexCacheObserver`: periodic Codex model-cache refresh (`~/.ductor/config/codex_models.json`).
- `UpdateObserver`: periodic PyPI version check + Telegram notification (upgradeable installs only).
- Rule-sync task: keeps existing `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` siblings mtime-synced inside `~/.ductor/workspace/`.
- Skill-sync task: syncs skills across `~/.ductor/workspace/skills/`, `~/.claude/skills/`, `~/.codex/skills/`, `~/.gemini/skills/`.

Optional network service:

- `ApiServer`: direct WebSocket + HTTP file endpoints (`/ws`, `/health`, `/files`, `/upload`).

## Startup Flow

### `ductor` (`ductor_bot/__main__.py`)

Default path:

1. `_is_configured()` checks token + allowed user IDs.
2. If unconfigured: run onboarding wizard (`init_wizard.run_onboarding()`).
3. If onboarding successfully installed a service, exit early.
4. Configure logging.
5. Load/create `~/.ductor/config/config.json`.
6. Deep-merge runtime config with `AgentConfig` defaults.
7. Run `init_workspace(paths)`.
8. Validate required config fields.
9. Acquire PID lock (`bot.pid`, `kill_existing=True`).
10. Start `TelegramBot`.

### `TelegramBot` startup (`ductor_bot/bot/app.py`)

1. Create orchestrator via `Orchestrator.create(config)`.
2. Fetch bot identity (`get_me`).
3. Consume restart sentinel and notify chat if present.
4. Attach cron, heartbeat, webhook result handlers and webhook wake handler.
5. Consume upgrade sentinel and notify chat if present.
6. Start `UpdateObserver` only for upgradeable installs.
7. Sync Telegram command list.
8. Start restart-marker watcher.

### `Orchestrator.create()` (`ductor_bot/orchestrator/core.py`)

1. Resolve paths from `ductor_home`.
2. Run `init_workspace(paths)` in a worker thread.
3. Set `DUCTOR_HOME` env var.
4. If Docker enabled: run `DockerManager.setup()` and keep recovery wiring.
5. If Docker container is active: re-sync skills in Docker-safe copy mode.
6. Inject runtime environment notice into workspace rule files (`inject_runtime_environment`).
7. Build orchestrator instance.
8. Check provider auth (`check_all_auth`) and set authenticated provider set.
9. Start `GeminiCacheObserver` (`~/.ductor/config/gemini_models.json`) and refresh runtime Gemini model registry from its callback.
10. Start `CodexCacheObserver` (`~/.ductor/config/codex_models.json`).
11. Create `CronObserver` and `WebhookObserver` with shared Codex cache.
12. Start cron, heartbeat, webhook, cleanup observers.
13. If `api.enabled=true`: start `ApiServer` (auto-generate token when empty, wire message/abort handlers and file context).
14. Start rule-sync and skill-sync watcher tasks.

`init_workspace()` is called in both `__main__.py` and `Orchestrator.create()`; behavior is idempotent.

## Message Routing

### Command ownership

- Bot-level handlers: `/start`, `/help`, `/info`, `/showfiles`, `/stop`, `/restart`, `/new`.
- Orchestrator command registry: `/new`, `/status`, `/model`, `/memory`, `/cron`, `/diagnose`, `/upgrade`.
- `/stop` is middleware/bot-local and does not route through orchestrator command dispatch.
- Quick-command bypass applies to `/status`, `/memory`, `/cron`, `/diagnose`, `/model`, `/showfiles`.
- `/showfiles` is handled directly in bot layer.
- `/model` bypass has busy check: when active work/queue exists, it returns immediate "agent is working" feedback.

### Directives (`ductor_bot/orchestrator/directives.py`)

- Only directives at message start are parsed.
- Model directive syntax: `@<model-id>`.
- Known model IDs come from:
  - `_CLAUDE_MODELS` (`haiku`, `sonnet`, `opus`)
  - `_GEMINI_ALIASES` (`auto`, `pro`, `flash`, `flash-lite`)
  - dynamically discovered Gemini model IDs from local Gemini CLI files.
- Other `@key` / `@key=value` directives are collected as raw directives.
- Directive-only messages (`@sonnet`) return guidance text instead of executing.

### Input security scan

`Orchestrator._handle_message_impl()` always runs `detect_suspicious_patterns(text)` before routing. Matches are logged as warnings; routing is not blocked at this layer.

## Normal Conversation Flow

`normal()` / `normal_streaming()` in `ductor_bot/orchestrator/flows.py`:

1. Determine requested model/provider.
2. Resolve session (`SessionManager.resolve_session`) with provider-isolated buckets.
3. New session only: append `MAINMEMORY.md` as `append_system_prompt`.
4. Apply message hooks (`MAINMEMORY_REMINDER` every 6th message).
5. Build `AgentRequest` with `resume_session` if available.
6. Gemini safeguard: if target provider is Gemini, auth mode is API-key, and `gemini_api_key` in config is empty/`"null"`, return warning text and skip CLI call.
7. Execute CLI (`CLIService.execute` or `execute_streaming`).
8. Error behavior:
   - recoverable:
     - SIGKILL: reset only active provider bucket and retry once.
     - invalid resumed session (`invalid session` / `session not found`): reset active provider bucket and retry once.
   - other errors: kill processes, preserve session, return session-error guidance.
9. On success: persist session ID (if changed), counters, cost/tokens, and optional session-age note.

## Streaming Path

Bot runtime path uses `bot/message_dispatch.py`:

1. `run_streaming_message()` creates stream editor + `StreamCoalescer`.
2. Orchestrator callbacks feed text/tool/system events.
3. System status mapping:
   - `thinking` -> `THINKING`
   - `compacting` -> `COMPACTING`
   - `recovering` -> `Please wait, recovering...`
4. Finalization:
   - flush coalescer,
   - finalize editor,
   - fallback/no-stream-content -> send full text with `send_rich`,
   - otherwise only send `<file:...>` outputs.

`CLIService.execute_streaming()` fallback behavior:

- checks `ProcessRegistry.was_aborted()` on each event, so `/stop` exits quickly,
- if stream errors or result event is missing:
  - aborted chat -> empty result,
  - non-error stream with accumulated text -> return accumulated text,
  - otherwise retry non-streaming and mark `stream_fallback=True`.

## Direct API Flow

`ApiServer` (`ductor_bot/api/server.py`) runs independently from aiogram and calls orchestrator callbacks directly.

Per connection:

1. `ws://<host>:<port>/ws` handshake.
2. First frame must be auth JSON with token (10s timeout).
3. Session `chat_id` defaults to first `allowed_user_ids` entry (fallback `1`); auth payload may override.
4. `message` frames run under per-`chat_id` lock and use streaming callbacks (`text_delta`, `tool_activity`, `system_status`, `result`).
5. `abort` frame or `/stop` message calls orchestrator abort path (`ProcessRegistry.kill_all`).

Additional HTTP endpoints:

- `GET /health` (no auth),
- `GET /files?path=...` (Bearer auth + `file_access` root checks),
- `POST /upload` (Bearer auth + multipart save to `workspace/api_files/YYYY-MM-DD/`).

Current wiring note: `config.api.chat_id` exists in schema but is not used by startup defaulting.

## Callback Query Flow

`TelegramBot._on_callback_query()`:

1. answer callback.
2. resolve welcome shortcut callbacks (`w:*`).
3. route special namespaces:
   - `mq:*` queue cancel,
   - `upg:*` upgrade flow,
   - `ms:*` model selector,
   - `crn:*` cron selector,
   - `sf:*` / `sf!` file browser.
4. generic callback path:
   - append `[USER ANSWER] ...` when possible,
   - acquire per-chat lock,
   - route callback payload through normal message pipeline.

Lock usage is path-dependent (e.g., queue cancel and upgrade callbacks are handled without acquiring the per-chat message lock).

## Background Systems

### Cron flow

- `CronObserver` watches `cron_jobs.json` mtime every 5 seconds.
- Uses timezone-aware scheduling (`job.timezone` -> `config.user_timezone` -> host TZ -> UTC).
- Execution path:
  - optional dependency lock,
  - quiet-hour gate,
  - validate task folder,
  - resolve task overrides (`provider`, `model`, `reasoning_effort`, `cli_parameters`),
  - build provider command,
  - run subprocess with timeout,
  - parse output,
  - persist status.
- Result delivery wiring:
  - `Orchestrator.set_cron_result_handler(...)` -> `CronObserver.set_result_handler(...)`
  - `TelegramBot._on_cron_result(...)` posts results to all allowed users.

### Heartbeat flow

- Observer loop runs every `interval_minutes`.
- Skips quiet hours and busy chats; performs stale process cleanup first.
- `heartbeat_flow()`:
  - read-only session lookup,
  - skip if no resumable session,
  - enforce cooldown,
  - execute heartbeat prompt with `resume_session`,
  - strip `ack_token`.
- ACK-only result is suppressed (no Telegram send, no session metric update).
- Non-ACK result updates session and is delivered by bot handler.

### Webhook flow

- `WebhookObserver.start()` auto-generates and persists global webhook token if empty.
- HTTP route: `POST /hooks/{hook_id}`.
- Validation chain: rate limit -> content-type -> JSON object -> hook exists/enabled -> per-hook auth.
- Valid requests return `202` immediately; dispatch runs async.
- Mode routing:
  - `wake`: uses bot wake handler (`_handle_webhook_wake`) and normal message pipeline under per-chat lock.
  - `cron_task`: runs one-shot provider execution in `cron_tasks/<task_folder>` with task overrides + quiet hours + dependency queue.
- Bot forwards only `cron_task` results from webhook result callback (`wake` responses already delivered by wake handler).

### Cleanup flow

- Hourly check in `user_timezone`.
- Runs at most once per day when local hour equals `cleanup.check_hour`.
- Deletes old top-level files in `workspace/telegram_files/`, `workspace/output_to_user/`, and `workspace/api_files/`.
- Deletion is non-recursive, so files inside date subdirectories (`YYYY-MM-DD/`) are not cleaned by current logic.

## Restart & Supervisor

### In-process restart triggers

- `/restart`: write restart sentinel, set exit code `42`, stop polling.
- Marker-based restart: if `restart-requested` file appears, set exit code `42` and stop polling.
- `__main__` restart handling:
  - when supervisor env is present (`DUCTOR_SUPERVISOR` or `INVOCATION_ID`), process exits with `42`,
  - otherwise process re-execs itself (`_re_exec_bot`) for direct foreground usage.

### Optional Supervisor (`ductor_bot/run.py`)

- Runs child process `python -m ductor_bot`.
- Optional hot-reload on `.py` changes (if `watchfiles` installed).
- Restart conditions:
  - exit `42` -> immediate restart,
  - file change -> restart child,
  - other crash -> exponential backoff.

This wrapper is optional and separate from the default `ductor` CLI entrypoint.
Detailed behavior: `docs/modules/supervisor.md`.

## Workspace Seeding Model

Template source:

- `ductor_bot/_home_defaults/` mirrors runtime `~/.ductor/` layout.

Copy rules in `workspace/init.py` (`_walk_and_copy`):

- Zone 2 (always overwrite):
  - `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
  - `.py` files in `workspace/tools/cron_tools/` and `workspace/tools/webhook_tools/`
- Zone 3 (seed once): all other files.
- `RULES*.md` templates are skipped in raw copy and deployed by `RulesSelector`.
- Hidden/ignored dirs are skipped.

Rule deployment (`workspace/rules_selector.py`):

- discovers template directories containing `RULES*.md`,
- selects variant by auth status:
  - `all-clis` when 2+ providers are authenticated,
  - otherwise one of `claude-only`, `codex-only`, `gemini-only`,
  - fallback `RULES.md`.
- deploys runtime files based on auth:
  - Claude -> `CLAUDE.md`
  - Codex -> `AGENTS.md`
  - Gemini -> `GEMINI.md`
- removes stale provider files for unauthenticated providers (except user-owned cron-task rule files).

## Logging Context

- `log_context.py` uses `ContextVar` fields (`operation`, `chat_id`, `session_id`) to enrich logs as `[op:chat_id:session_id_8]`.
- ingress operation labels: `msg`, `cb`, `cron`, `hb`, `wh`, `api`.
- `logging_config.py` configures colored console logs and rotating file logs (`~/.ductor/logs/agent.log`).

## Core Design Trade-offs

- JSON files over DB: transparent and easy to inspect.
- In-process observers: simple deployment, lifecycle tied to bot process.
- Per-chat lock + queue tracking: strong ordering and race prevention at chat level.
- Stream coalescing + edit mode: better Telegram UX with controlled update frequency.
