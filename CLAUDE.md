This file gives coding agents a current map of the repository.

## Project Overview

ductor is a messaging bot that routes chat input to official provider CLIs (`claude`, `codex`, `gemini`), streams responses back via Telegram or Matrix, persists per-chat state, and runs cron/webhook/heartbeat automation in-process.

Stack:

- Python 3.11+
- aiogram 3.x
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
pytest tests/messenger/telegram/test_app.py
pytest -k "pattern"

# Quality
ruff format .
ruff check .
mypy ductor_bot
```

## Runtime Flow

```text
Telegram Update
  -> AuthMiddleware
  -> SequentialMiddleware (queue + per-chat lock)
  -> TelegramBot handlers (messenger/telegram/)
  -> Orchestrator
  -> CLIService
  -> provider subprocess (claude/codex/gemini)
  -> Telegram output (stream edit or one-shot)
```

## Module Map

| Module | Purpose |
|---|---|
| `messenger/` | Transport-agnostic protocols, capabilities, notifications, registry |
| `messenger/telegram/` | Telegram transport: handlers, streaming, inline keyboards, queue UX |
| `messenger/matrix/` | Matrix transport: segment streaming, reaction buttons |
| `orchestrator/` | command registry, directives/hooks, flow routing, observer wiring |
| `cli/` | provider wrappers, stream parsing, auth checks, process registry, model caches |
| `session/` | chat sessions with provider-isolated buckets |
| `background/` | named background sessions (`/session`) with follow-ups |
| `cron/` | in-process scheduler and one-shot task execution |
| `webhook/` | HTTP hooks (`wake` and `cron_task`) |
| `heartbeat/` | periodic proactive checks in active sessions |
| `cleanup/` | daily retention cleanup |
| `workspace/` | home seeding, rules deployment/sync, skill sync |
| `multiagent/` | multi-agent supervisor, inter-agent bus, shared knowledge, health monitoring |
| `infra/` | PID lock, service backends, Docker manager, update/restart helpers |

## Key Runtime Patterns

- `DuctorPaths` (`workspace/paths.py`) is the single source of truth for paths.
- Workspace init is zone-based:
  - Zone 2 overwrite: `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, and framework cron/webhook tool scripts.
  - Zone 3 seed-once for user-owned files.
- Rules are selected from `RULES*.md` variants and deployed per authenticated provider.
- Rule sync updates existing `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` siblings recursively by mtime.
- Skill sync spans `~/.ductor/workspace/skills`, `~/.claude/skills`, `~/.codex/skills`, `~/.gemini/skills`.
  - normal mode: links
  - Docker mode: managed copies (`.ductor_managed` marker)
- Streaming fallback is automatic; `/stop` abort checks are enforced during event loop processing.
- Session state is provider-isolated; `/new` resets only the active provider bucket.

## Background Systems

All run as in-process asyncio tasks:

- `BackgroundObserver`
- `CronObserver`
- `HeartbeatObserver`
- `WebhookObserver`
- `CleanupObserver`
- `CodexCacheObserver`
- `GeminiCacheObserver`
- rule sync watcher
- skill sync watcher
- update observer (upgradeable installs)

Optional multi-agent system (when `agents.json` is present):

- `AgentSupervisor` (manages main + sub-agents with crash recovery)
- `InterAgentBus` (in-memory sync + async messaging)
- `InternalAgentAPI` (`127.0.0.1:8799`, bridges CLI tools to bus)
- `SharedKnowledgeSync` (`SHAREDMEMORY.md` -> all agents' `MAINMEMORY.md`)
- `FileWatcher` on `agents.json` (auto-detect add/remove/change)

## Service Backends

- Linux: systemd user service
- macOS: launchd Launch Agent
- Windows: Task Scheduler

`ductor service logs` behavior:

- Linux: `journalctl --user -u ductor -f`
- macOS/Windows: recent lines from `~/.ductor/logs/agent.log` (fallback newest `*.log`)

## CLI Commands

| Command | Effect |
|---|---|
| `ductor` | Start bot (runs onboarding if needed) |
| `ductor stop` | Stop bot and Docker container |
| `ductor restart` | Restart bot |
| `ductor upgrade` | Stop, upgrade, restart |
| `ductor docker rebuild` | Stop bot, remove container & image, rebuilt on next start |
| `ductor docker enable` | Set `docker.enabled = true` |
| `ductor docker disable` | Stop container, set `docker.enabled = false` |
| `ductor service install` | Install as background service |
| `ductor service [sub]` | Service management (status/stop/logs/...) |
| `ductor agents` | List all sub-agents and their config |
| `ductor agents add <name>` | Add a new sub-agent (interactive) |
| `ductor agents remove <name>` | Remove a sub-agent |

## Data Files (`~/.ductor`)

- `config/config.json`
- `.env` (external API secrets, injected into all CLI subprocesses)
- `sessions.json`
- `cron_jobs.json`
- `webhooks.json`
- `agents.json`
- `SHAREDMEMORY.md`
- `agents/<name>/` (sub-agent workspaces)
- `logs/agent.log`

## Conventions

- `asyncio_mode = "auto"` in tests
- line length 100
- mypy strict mode
- ruff with strict lint profile
- config deep-merge adds new defaults without dropping user keys
- supervisor restart code is `42`
