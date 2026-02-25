# Configuration

Runtime config file: `~/.ductor/config/config.json`.

Seed source: `<repo>/config.example.json` (source checkout) or packaged fallback `ductor_bot/_config_example.json` (installed mode).

## Config Creation

Primary path: `ductor onboarding` (interactive wizard) writes `config.json` with user-provided values merged into `AgentConfig` defaults.

## Load & Merge Behavior

Config is merged in two places:

1. `ductor_bot/__main__.py::load_config()`
   - creates config on first start (copy from `config.example.json` or Pydantic defaults),
   - deep-merges runtime file with `AgentConfig` defaults,
   - writes back only when new keys were added.
2. `ductor_bot/workspace/init.py::_smart_merge_config()`
   - shallow merge `{**defaults, **existing}` with `config.example.json`,
   - preserves existing user top-level keys,
   - fills missing top-level keys from `config.example.json`.

Normalization detail:

- onboarding and runtime config load normalize `gemini_api_key` default to string `"null"` in persisted JSON for backward compatibility.
- `AgentConfig` validator converts null-like text (`""`, `"null"`, `"none"`) to `None` at runtime.

Runtime edits persisted through config helpers include `/model` changes (model/provider/reasoning), webhook token auto-generation, and API token auto-generation.

## `AgentConfig` (`ductor_bot/config.py`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `log_level` | `str` | `"INFO"` | Applied at startup unless CLI `--verbose` is used |
| `provider` | `str` | `"claude"` | Default provider |
| `model` | `str` | `"opus"` | Default model ID |
| `ductor_home` | `str` | `"~/.ductor"` | Runtime home root |
| `idle_timeout_minutes` | `int` | `1440` | Session freshness idle timeout (`0` disables idle expiry) |
| `session_age_warning_hours` | `int` | `12` | Adds `/new` reminder after threshold (every 10 messages) |
| `daily_reset_hour` | `int` | `4` | Daily reset boundary hour in `user_timezone` |
| `daily_reset_enabled` | `bool` | `false` | Enables daily session reset checks |
| `user_timezone` | `str` | `""` | IANA timezone used by cron/heartbeat/cleanup/session reset |
| `max_budget_usd` | `float \| None` | `None` | Passed to Claude CLI |
| `max_turns` | `int \| None` | `None` | Passed to Claude CLI |
| `max_session_messages` | `int \| None` | `None` | Session rollover limit |
| `permission_mode` | `str` | `"bypassPermissions"` | Provider sandbox/approval mode |
| `cli_timeout` | `float` | `600.0` | Timeout per CLI call (seconds) |
| `reasoning_effort` | `str` | `"medium"` | Default Codex reasoning level |
| `file_access` | `str` | `"all"` | File access scope (`all`, `home`, `workspace`) for Telegram sends and API `GET /files` |
| `gemini_api_key` | `str \| None` | `None` | Config fallback key injected for Gemini API-key mode |
| `telegram_token` | `str` | `""` | Telegram bot token |
| `allowed_user_ids` | `list[int]` | `[]` | Telegram allowlist |
| `streaming` | `StreamingConfig` | see below | Streaming tuning |
| `docker` | `DockerConfig` | see below | Docker sidecar config |
| `heartbeat` | `HeartbeatConfig` | see below | Background heartbeat config |
| `cleanup` | `CleanupConfig` | see below | Daily file-retention cleanup |
| `webhooks` | `WebhookConfig` | see below | Webhook HTTP server config |
| `api` | `ApiConfig` | see below | Direct WebSocket API server config |
| `cli_parameters` | `CLIParametersConfig` | see below | Provider-specific extra CLI flags |

## `CLIParametersConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `claude` | `list[str]` | `[]` | Extra args appended to Claude CLI command |
| `codex` | `list[str]` | `[]` | Extra args appended to Codex CLI command |
| `gemini` | `list[str]` | `[]` | Extra args appended to Gemini CLI command |

Used by `CLIServiceConfig` for main-chat calls.

Automation note:

- cron/webhook `cron_task` runs use task-level `cli_parameters` from `cron_jobs.json` / `webhooks.json` (no merge with global `cli_parameters`).

## Task-Level Automation Overrides

Stored outside `config.json` in:

- `~/.ductor/cron_jobs.json` (`CronJob`)
- `~/.ductor/webhooks.json` (`WebhookEntry`, `cron_task` mode)

Common per-task fields:

- execution: `provider`, `model`, `reasoning_effort`, `cli_parameters`
- scheduling guards: `quiet_start`, `quiet_end`, `dependency`

Cron-only field:

- `timezone` (per-job timezone override)

Behavior notes:

- missing execution fields fall back to global config via `resolve_cli_config()`,
- `dependency` is global across cron + webhook `cron_task` runs (shared `DependencyQueue`),
- quiet-hour checks fall back to global heartbeat quiet settings when per-task values are omitted.

## `StreamingConfig`

| Field | Type | Default |
|---|---|---|
| `enabled` | `bool` | `true` |
| `min_chars` | `int` | `200` |
| `max_chars` | `int` | `4000` |
| `idle_ms` | `int` | `800` |
| `edit_interval_seconds` | `float` | `2.0` |
| `max_edit_failures` | `int` | `3` |
| `append_mode` | `bool` | `false` |
| `sentence_break` | `bool` | `true` |

## `DockerConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `image_name` | `str` | `"ductor-sandbox"` | Docker image name |
| `container_name` | `str` | `"ductor-sandbox"` | Docker container name |
| `auto_build` | `bool` | `true` | Build image automatically when missing |
| `mount_host_cache` | `bool` | `false` | Mount host `~/.cache` into container (see below) |

`Orchestrator.create()` calls `DockerManager.setup()` when enabled. If setup fails, ductor logs warning and falls back to host execution.

### `mount_host_cache`

Mounts the host's platform-specific cache directory into the container at `/home/node/.cache`:

| Platform | Host path |
|---|---|
| Linux | `~/.cache` (or `$XDG_CACHE_HOME`) |
| macOS | `~/Library/Caches` |
| Windows | `%LOCALAPPDATA%` |

Use case: browser-based skills (e.g. google-ai-mode) that use patchright/playwright need access to persistent browser profiles and browser binaries stored in the host cache. Without this, each container start requires a fresh CAPTCHA solve and Chrome download.

Disabled by default because it exposes the host cache directory to the sandbox.

## `HeartbeatConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `interval_minutes` | `int` | `30` | Loop interval |
| `cooldown_minutes` | `int` | `5` | Skip if user active recently |
| `quiet_start` | `int` | `21` | Quiet start hour in `user_timezone` |
| `quiet_end` | `int` | `8` | Quiet end hour in `user_timezone` |
| `prompt` | `str` | default prompt | Multiline default prompt references `MAINMEMORY.md` and `cron_tasks/` |
| `ack_token` | `str` | `"HEARTBEAT_OK"` | Suppression token |

## `CleanupConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `true` | Master toggle |
| `telegram_files_days` | `int` | `30` | Retention in `workspace/telegram_files/` |
| `output_to_user_days` | `int` | `30` | Retention in `workspace/output_to_user/` |
| `api_files_days` | `int` | `30` | Retention in `workspace/api_files/` |
| `check_hour` | `int` | `3` | Local hour in `user_timezone` for cleanup run |

Cleanup implementation detail:

- cleanup is non-recursive (`_delete_old_files` checks only top-level files),
- media/API uploads are stored in date subdirectories (`.../YYYY-MM-DD/...`), so those uploaded files are currently not deleted by this observer.

## `WebhookConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `host` | `str` | `"127.0.0.1"` | Bind address (localhost by default) |
| `port` | `int` | `8742` | HTTP server port |
| `token` | `str` | `""` | Global bearer fallback token (auto-generated when webhooks start) |
| `max_body_bytes` | `int` | `262144` | Max request body size |
| `rate_limit_per_minute` | `int` | `30` | Sliding-window rate limit |

## `ApiConfig`

| Field | Type | Default | Notes |
|---|---|---|---|
| `enabled` | `bool` | `false` | Master toggle |
| `host` | `str` | `"0.0.0.0"` | Bind address |
| `port` | `int` | `8741` | API HTTP/WebSocket port |
| `token` | `str` | `""` | Bearer/WebSocket auth token (auto-generated when API starts) |
| `chat_id` | `int` | `0` | Present in config schema; see runtime note below |
| `allow_public` | `bool` | `false` | Suppresses Tailscale-not-detected warning |

Runtime note (`Orchestrator._start_api_server` + `ApiServer._authenticate`):

- default API session chat ID currently comes from first `allowed_user_ids` entry (fallback `1`),
- per-connection auth payload may override via `{"type":"auth","chat_id":...}`,
- `config.api.chat_id` is currently not consumed by orchestrator startup wiring.

## Model Resolution

`ModelRegistry` (`ductor_bot/config.py`):

- Claude models are hardcoded: `haiku`, `sonnet`, `opus`.
- Gemini aliases are hardcoded: `auto`, `pro`, `flash`, `flash-lite`.
- Runtime Gemini models are discovered from local Gemini CLI files at startup.
- Provider resolution (`provider_for(model_id)`):
  - Claude when in `_CLAUDE_MODELS`,
  - Gemini when in aliases/discovered set or when model looks like `gemini-*`/`auto-gemini-*`,
  - otherwise Codex.

## Timezone Resolution

`resolve_user_timezone(configured)` in `ductor_bot/config.py`:

1. valid configured IANA timezone,
2. `$TZ` env var,
3. host system detection:
   - Windows: local datetime tzinfo,
   - POSIX: `/etc/localtime` symlink,
4. fallback `UTC`.

Returns `zoneinfo.ZoneInfo` and is used by cron scheduling, session daily-reset checks, heartbeat quiet hours, and cleanup scheduling.

## `reasoning_effort`

UI values: `low`, `medium`, `high`, `xhigh`.

Main-chat flow:

`AgentConfig` -> `CLIServiceConfig` -> `CLIConfig` -> `CodexCLI` (`-c model_reasoning_effort=<value>` when relevant).

Automation flow:

- `resolve_cli_config()` applies reasoning effort only for Codex models that support the requested effort.

## Codex Model Cache

Path: `~/.ductor/config/codex_models.json`.

Behavior:

- loaded at orchestrator startup (`CodexCacheObserver.start()`),
- startup load is forced refresh (`force_refresh=True`),
- checked hourly in background,
- `load_or_refresh()` uses cache if `<24h` old, otherwise re-discovers via Codex app server,
- consumed by `/model` wizard, `resolve_cli_config()` for cron/webhook validation, and `/diagnose` output.

## Gemini Model Cache

Path: `~/.ductor/config/gemini_models.json`.

Behavior:

- loaded at orchestrator startup (`GeminiCacheObserver.start()`),
- startup load uses cached data when fresh and refreshes only when stale/missing,
- refreshed hourly in background,
- refresh callback updates runtime Gemini model registry (`set_gemini_models(...)`) used by directives and model selector.
