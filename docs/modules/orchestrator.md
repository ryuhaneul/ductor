# orchestrator/

Central routing layer between ingress transports (Telegram + optional API server) and CLI execution.

## Files

- `core.py`: `Orchestrator` lifecycle, routing, observer wiring, shutdown
- `registry.py`: `CommandRegistry`, `OrchestratorResult`
- `commands.py`: command handlers (`/status`, `/model`, `/cron`, `/diagnose`, `/upgrade`, ...)
- `flows.py`: normal flow, streaming flow, heartbeat flow, session-recovery/error handling
- `directives.py`: leading `@...` directive parser
- `hooks.py`: hook registry + `MAINMEMORY_REMINDER`
- `model_selector.py`: interactive model/provider switch wizard (`ms:*`)
- `cron_selector.py`: interactive cron toggles (`crn:*`)
- API server integration points in `core.py`: `_start_api_server()`, `_api_server`, shutdown stop path

## Startup (`Orchestrator.create`)

1. `init_workspace(paths)`
2. set `DUCTOR_HOME`
3. optional Docker setup (`DockerManager.setup`)
4. if Docker active: re-sync skills in copy mode (`docker_active=True`)
5. inject runtime environment notice into workspace rule files
6. construct orchestrator instance
7. detect provider auth (`check_all_auth`) and update available providers
8. start model caches (`_init_model_caches`):
   - `GeminiCacheObserver` (`gemini_models.json`) with refresh callback to `set_gemini_models`
   - `CodexCacheObserver` (`codex_models.json`)
9. construct `CronObserver` + `WebhookObserver` (heartbeat/cleanup already constructed in `__init__`)
10. start `CronObserver`, `HeartbeatObserver`, `WebhookObserver`, `CleanupObserver`
11. if `config.api.enabled`: start `ApiServer` via `_start_api_server`
12. start rule sync watcher (`watch_rule_files`)
13. start skill sync watcher (`watch_skill_sync`)

## Routing entry points

- `handle_message(chat_id, text)`
- `handle_message_streaming(chat_id, text, callbacks...)`

Shared path:

- clear abort flag
- log suspicious input patterns (no hard block here)
- command dispatch first
- fallback to directive + normal/streaming flow
- domain/unexpected exception boundary returns generic error text

## Command registry

Registered commands:

- `/new`
- `/status`
- `/model`
- `/model ` (prefix form)
- `/memory`
- `/cron`
- `/diagnose`
- `/upgrade`

`/stop` is intentionally not registered here; abort is middleware/bot-level behavior.

Note:

- `/new` is also handled directly in the bot layer (`TelegramBot._on_new`) via `reset_active_provider_session`.
- keeping `/new` registered in orchestrator preserves behavior for non-bot entry paths that still route through command dispatch.

## Directives

`parse_directives(text, known_models)` parses only leading `@...` tokens.

Known model IDs are refreshed from:

- Claude set (`haiku`, `sonnet`, `opus`)
- Gemini aliases (`auto`, `pro`, `flash`, `flash-lite`)
- discovered Gemini models from runtime cache

Codex IDs are not included in inline directive-known set.

Directive-only model messages return guidance text instead of executing.

## Normal/streaming flow (`flows.py`)

`_prepare_normal()`:

- resolve runtime model/provider target
- resolve or create session with provider-isolated buckets
- new session: append `MAINMEMORY.md` as system appendix
- apply hooks
- build `AgentRequest`

Gemini safeguard:

- if target provider is Gemini,
- and Gemini auth mode is API-key,
- and `gemini_api_key` in config is empty/`"null"`,
- return warning result without spawning CLI.

Error behavior:

- recoverable errors:
  - SIGKILL -> reset active provider bucket and retry once
  - invalid resumed session (`invalid session` / `session not found`) -> reset active provider bucket and retry once
- other errors: kill processes, preserve session, return session-error guidance

Success behavior:

- persist returned session ID
- increment counters/cost/tokens
- optional session-age note every 10 messages after threshold

## Heartbeat flow

`heartbeat_flow` is read-only until non-ACK output:

- skip when no active session or no `session_id`
- skip when provider mismatch or cooldown not reached
- run heartbeat prompt in existing session
- strip ACK token (`HEARTBEAT_OK` by default)
- only non-ACK responses update session and trigger delivery

Observer wiring in `Orchestrator.__init__`:

- busy check callback -> `ProcessRegistry.has_active` (heartbeat skips while chat has active CLI process)
- stale cleanup callback -> `ProcessRegistry.kill_stale(config.cli_timeout * 2)` (run before heartbeat ticks)

## Model selector (`model_selector.py`)

Callback namespace: `ms:`

- provider step: `ms:p:<provider>`
- model step: `ms:m:<model>`
- codex reasoning step: `ms:r:<effort>:<model>`
- back: `ms:b:*`

Behavior:

- provider buttons shown only for authenticated providers
- model list sources:
  - Claude static list
  - Codex cache
  - Gemini discovered models
- switch updates config + CLIService defaults
- provider session buckets are preserved across switches

## Cron selector (`cron_selector.py`)

Callback namespace: `crn:`

- supports paging, refresh, per-job toggle, bulk all-on/all-off
- toggles persist in `CronManager` and call `CronObserver.reschedule_now()`

## Webhook wiring

`Orchestrator` only wires handlers; wake execution remains in bot layer:

- `set_webhook_result_handler`
- `set_webhook_wake_handler`

This keeps wake dispatch behind the same per-chat lock as normal messages.

## API wiring

`_start_api_server()` in `core.py`:

1. auto-generates `api.token` when empty and persists it to `config.json`
2. computes default API `chat_id` from first `allowed_user_ids` entry (fallback `1`)
3. constructs `ApiServer(config.api, default_chat_id=...)`
4. wires callbacks:
   - message streaming -> `handle_message_streaming`
   - abort -> `abort`
5. wires file context:
   - `allowed_roots` from `resolve_allowed_roots(config.file_access, paths.workspace)`
   - upload directory `paths.api_files_dir`
   - workspace root for relative prompt paths
6. starts aiohttp server

Note:

- `config.api.chat_id` exists in schema but is currently not consumed by startup wiring.
- clients can still override session via auth payload `{"type":"auth","chat_id":...}`.

## Shutdown

`Orchestrator.shutdown()`:

1. stop API server if running
2. cancel rule/skill watcher tasks
3. `cleanup_ductor_links(paths)`
4. stop heartbeat/webhook/cron/cleanup observers
5. stop codex and gemini cache observers
6. teardown Docker container (if managed)
