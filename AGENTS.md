This file gives coding agents a current map of the repository.

## Project Overview

ductor is a multi-transport chat orchestrator for official provider CLIs (`claude`, `codex`, `gemini`).
It supports Telegram and Matrix, optional direct WebSocket API ingress, in-process automation (cron/webhook/heartbeat/cleanup), and multi-agent runtime (main + sub-agents) under one supervisor.

Stack:

- Python 3.11+
- aiogram 3.x (Telegram)
- matrix-nio (Matrix, optional extra)
- aiohttp (webhook + internal API + optional direct API)
- Pydantic 2.x
- asyncio

## Development Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run
ductor
ductor -v

# Tests
pytest
pytest -k "pattern"

# Quality
ruff format .
ruff check .
mypy ductor_bot
```

## Runtime Flow

```text
Telegram:
  Update -> AuthMiddleware -> SequentialMiddleware -> TelegramBot
  -> Orchestrator -> CLIService -> provider subprocess -> Telegram delivery

Matrix:
  sync event -> MatrixBot auth/room checks -> Orchestrator
  -> CLIService -> provider subprocess -> Matrix delivery

API (optional):
  /ws auth (token + e2e_pk) -> encrypted frames
  -> Orchestrator streaming -> encrypted result events
```

Background and async delivery:

```text
Observer/TaskHub/InterAgentBus callback
  -> bus.adapters -> Envelope -> MessageBus
  -> optional lock + optional session injection
  -> transport adapters (TelegramTransport / MatrixTransport)
```

## Module Map

| Module | Purpose |
|---|---|
| `cli_commands/` | CLI command implementations (lifecycle, service, docker, api, agents, install) |
| `messenger/telegram/` | Telegram transport (handlers, middleware, startup, file/media UX) |
| `messenger/matrix/` | Matrix transport (commands, streaming, reaction buttons, media) |
| `messenger/` | transport protocol/capabilities/registry + multi-transport adapter |
| `orchestrator/` | command routing, directives/hooks, conversation flows, provider/observer wiring |
| `bus/` | unified `Envelope` + `MessageBus` + shared `LockPool` delivery pipeline |
| `cli/` | provider wrappers, stream parsing, auth checks, model caches, process registry |
| `session/` | `SessionKey(chat_id, topic_id)`, provider-isolated session buckets, named sessions |
| `tasks/` | delegated background task runtime (`TaskHub`) + persistence |
| `multiagent/` | supervisor, inter-agent bus, localhost internal API bridge, shared knowledge sync |
| `api/` | optional direct WebSocket API + authenticated file endpoints |
| `cron/`, `webhook/`, `heartbeat/`, `cleanup/`, `background/` | in-process automation observers |
| `workspace/` | `~/.ductor` path model, initialization, rule deployment/sync, skill sync |
| `infra/` | pid lock, service backends, docker manager, restart/update/recovery helpers |
| `files/`, `security/`, `text/` | shared file/path safety, prompt safety, response formatting |

## Key Runtime Patterns

- `DuctorPaths` (`workspace/paths.py`) is the single source of truth for runtime paths.
- Session identity is `SessionKey(chat_id, topic_id)` across Telegram topics, Matrix rooms (mapped int), and API channel isolation.
- `/new` resets only the active provider bucket for the active session key.
- `MessageBus` is the single async delivery path for observers, task callbacks, and inter-agent async responses.
- One shared `LockPool` is used by Telegram middleware, API server, and `MessageBus`.
- Workspace init is zone-based:
  - Zone 2 overwrite: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, framework-managed tool scripts
  - Zone 3 seed-once: user-owned files
- Rule sync is mtime-based for sibling `CLAUDE.md`/`AGENTS.md`/`GEMINI.md`; cron task folders additionally get missing rule backfill.
- Skill sync spans `~/.ductor/workspace/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.gemini/skills`:
  - normal mode: links/junctions
  - Docker mode: managed copies (`.ductor_managed`)

## Background Systems

All run as in-process asyncio tasks:

- `BackgroundObserver` (named sessions)
- `CronObserver`
- `WebhookObserver`
- `HeartbeatObserver`
- `CleanupObserver`
- `CodexCacheObserver`
- `GeminiCacheObserver`
- config reloader
- rule sync watcher
- skill sync watcher
- update observer (upgradeable installs)

## Service Backends

- Linux: systemd user service
- macOS: launchd Launch Agent
- Windows: Task Scheduler

`ductor service logs` behavior:

- Linux: `journalctl --user -u ductor -f`
- macOS/Windows: recent lines from `~/.ductor/logs/agent.log` (fallback newest `*.log`)

## CLI Surface

Core:

- `ductor`, `ductor onboarding`, `ductor reset`
- `ductor status`, `ductor stop`, `ductor restart`, `ductor upgrade`, `ductor uninstall`

Groups:

- `ductor service <install|status|start|stop|logs|uninstall>`
- `ductor docker <rebuild|enable|disable|mount|unmount|mounts|extras|extras-add|extras-remove>`
- `ductor api <enable|disable>`
- `ductor agents <list|add|remove>`
- `ductor install <matrix|api>`

## Key Data Files (`~/.ductor`)

- `config/config.json`
- `sessions.json`
- `named_sessions.json`
- `tasks.json`
- `cron_jobs.json`
- `webhooks.json`
- `agents.json`
- `startup_state.json`
- `inflight_turns.json`
- `chat_activity.json`
- `logs/agent.log`

## Conventions

- `asyncio_mode = "auto"` in tests
- line length 100
- mypy strict mode
- ruff strict lint profile
- config deep-merge adds new defaults without dropping user keys
- supervisor restart code is `42`
