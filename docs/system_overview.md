# System Overview

Fastest end-to-end mental model for ductor.

## 1) Runtime shape

One Python process hosts:

- main agent stack (always)
- optional sub-agent stacks from `~/.ductor/agents.json`
- shared `AgentSupervisor`
- shared `InterAgentBus`
- shared internal HTTP bridge (`InternalAgentAPI`, port `8799`)
- shared `TaskHub` when `tasks.enabled=true`

Each agent stack contains:

- Transport bot (`TelegramBot` or `MatrixBot`, selected via
  `config.transport`; `MultiBotAdapter` enables parallel
  multi-transport execution when `config.transports` lists
  multiple entries)
- `Orchestrator` (routing + flows)
- `CLIService` (provider wrappers)
- provider subprocesses (`claude`, `codex`, `gemini`)

## 2) Primary message path

```text
Telegram:                                 Matrix:
  Telegram update                           Matrix sync event
  -> AuthMiddleware                         -> room/user allowlist check
  -> SequentialMiddleware                   -> MatrixBot handler
  -> bot handlers                           -> Orchestrator.handle_message(_streaming)
  -> Orchestrator.handle_message(_streaming)-> CLIService
  -> CLIService                             -> provider subprocess
  -> provider subprocess                    -> Matrix room message
  -> Telegram response (stream edits)
```

Notes:

- `/stop` and `/stop_all` are middleware/bot-level abort paths (not orchestrator command dispatch).
- `/new` resets only the active provider bucket for the active session key.
- Telegram groups: both `allowed_group_ids` and `allowed_user_ids` must allow the message.
- `group_mention_only` behavior differs by transport:
  - Telegram: mention/reply gating only (no auth bypass)
  - Matrix non-DM rooms: user allowlist check is bypassed; room allowlist + mention/reply are used as the gate

## 3) Session identity model

Session identity is transport-agnostic via `SessionKey(chat_id, topic_id)`.

- Telegram normal chats: `topic_id=None`
- Telegram forum topics: `topic_id=message_thread_id`
- API channel isolation: `topic_id=channel_id` (from auth payload)

Persistence key format in `sessions.json`:

- legacy flat: `"<chat_id>"`
- topic-aware: `"<chat_id>:<topic_id>"`

This keeps topic/channel conversations fully isolated while staying backward-compatible.

## 4) Background and delivery model

Observers run in-process (cron, webhook, heartbeat, cleanup, background sessions, model caches, config watcher, rule/skill sync).

All observer/task/inter-agent results now flow through `bus/`:

- wrap to `Envelope` (`bus/adapters.py`)
- route via `MessageBus`
- optional lock + optional injection into active session
- deliver through registered transport (Telegram or Matrix)

A single shared `LockPool` is used by Telegram middleware, API server, and message bus.

## 5) Optional direct API path

When `api.enabled=true` and PyNaCl is installed:

```text
/ws
  -> plaintext auth frame (token + e2e_pk + optional chat_id/channel_id)
  -> auth_ok
  -> encrypted frames (NaCl Box)
  -> orchestrator streaming callbacks
```

HTTP endpoints:

- `GET /health`
- `GET /files?path=...` (Bearer token + root checks)
- `POST /upload` (Bearer token, multipart)

## 6) Internal localhost bridge

`InternalAgentAPI` endpoints for CLI tool scripts:

- `/interagent/send`
- `/interagent/send_async`
- `/interagent/agents`
- `/interagent/health`
- `/tasks/create`
- `/tasks/resume`
- `/tasks/ask_parent`
- `/tasks/list`
- `/tasks/cancel`
- `/tasks/delete`

Ownership checks are enforced for resume/cancel/delete when `from=<agent>` is supplied.

## 7) Key runtime files (`~/.ductor`)

- `config/config.json`
- `sessions.json`
- `named_sessions.json`
- `tasks.json`
- `chat_activity.json`
- `cron_jobs.json`
- `webhooks.json`
- `agents.json`
- `startup_state.json`
- `inflight_turns.json`
- `SHAREDMEMORY.md`
- `logs/agent.log`
- `workspace/` (rules, tools, files, tasks, cron_tasks, skills)

Sub-agent home: `~/.ductor/agents/<name>/` with its own config/workspace/session files.

## 8) Where to read code first

1. `ductor_bot/__main__.py` (entrypoint + config/load/run)
2. `ductor_bot/cli_commands/` (actual CLI subcommand logic)
3. `ductor_bot/multiagent/supervisor.py` (always-on runtime wrapper)
4. `ductor_bot/messenger/telegram/app.py` + `messenger/telegram/startup.py` (Telegram), `ductor_bot/messenger/matrix/bot.py` (Matrix)
5. `ductor_bot/orchestrator/core.py` + `orchestrator/lifecycle.py`
6. `ductor_bot/bus/*` (unified delivery/injection)
7. `ductor_bot/tasks/hub.py` + `tasks/registry.py`
8. `ductor_bot/cli/service.py` and provider wrappers

## 9) Command surface (high level)

Chat commands (Telegram and Matrix):

- `/new`, `/stop`, `/stop_all`, `/model`, `/status`, `/memory`, `/session`, `/sessions`, `/tasks`, `/cron`, `/diagnose`, `/upgrade`
- Telegram-only utility commands: `/where`, `/leave` (work but are not in command popup)
- Matrix uses `!` prefix by default (e.g. `!help`, `!status`); `/` also works but may conflict with Element's built-in commands

Main-agent only (chat commands):

- Telegram: `/agents`, `/agent_start`, `/agent_stop`, `/agent_restart`, `/agent_commands`
- Matrix: `!agents`, `!agent_start`, `!agent_stop`, `!agent_restart`, `!agent_commands` (`/` prefix also supported)

CLI:

- `ductor`
- `ductor status|stop|restart|upgrade|uninstall|onboarding|reset|help`
- `ductor service ...`
- `ductor docker ...` (includes `extras`, `extras-add`, `extras-remove` for optional AI/ML packages)
- `ductor api ...`
- `ductor agents ...`
- `ductor install <extra>` (`matrix`, `api`)
