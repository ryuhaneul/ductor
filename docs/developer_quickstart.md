# Developer Quickstart

Fast onboarding path for contributors and junior devs.

## 1) Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional for full runtime validation:

- install/auth at least one provider CLI (`claude`, `codex`, or `gemini`)
- create Telegram bot token + user ID

## 2) Run the bot

```bash
ductor
```

First run auto-starts onboarding and writes config to `~/.ductor/config/config.json`.

Primary runtime files/directories:

- `~/.ductor/sessions.json`
- `~/.ductor/cron_jobs.json`
- `~/.ductor/webhooks.json`
- `~/.ductor/workspace/`
- `~/.ductor/logs/agent.log`

## 3) Quality gates

```bash
pytest
ruff format .
ruff check .
mypy ductor_bot
```

Expected: zero warnings, zero errors.

## 4) Core mental model

```text
Telegram update or API message
  -> ingress layer (bot middleware/handlers or ApiServer)
  -> orchestrator (routing + flows)
  -> CLI service (claude/codex/gemini subprocess)
  -> streamed/non-streamed response to Telegram or API client
```

Background systems run in-process:

- cron
- heartbeat
- webhook
- cleanup
- codex model cache
- gemini model cache
- rule sync
- skill sync
- update check (upgradeable installs only)

Optional network service:

- direct API server (`ApiServer`) when `api.enabled=true`

## 5) Read order in code

Entry points:

- `ductor_bot/__main__.py`
- `ductor_bot/bot/app.py`
- `ductor_bot/orchestrator/core.py`

Hot paths:

- queue/lock behavior: `ductor_bot/bot/middleware.py`
- message flows: `ductor_bot/orchestrator/flows.py`
- command handling: `ductor_bot/orchestrator/commands.py`
- provider execution: `ductor_bot/cli/service.py`
- provider wrappers: `ductor_bot/cli/claude_provider.py`, `ductor_bot/cli/codex_provider.py`, `ductor_bot/cli/gemini_provider.py`
- direct API ingress: `ductor_bot/api/server.py`
- shared file helpers: `ductor_bot/files/allowed_roots.py`, `ductor_bot/files/tags.py`, `ductor_bot/files/storage.py`, `ductor_bot/files/prompt.py`
- workspace/rules/skills: `ductor_bot/workspace/init.py`, `ductor_bot/workspace/rules_selector.py`, `ductor_bot/workspace/skill_sync.py`

## 6) Common debug paths

If message handling is wrong:

1. `ductor_bot/bot/middleware.py`
2. `ductor_bot/bot/app.py`
3. `ductor_bot/orchestrator/core.py`
4. `ductor_bot/cli/service.py`

If direct API is wrong:

1. `ductor_bot/api/server.py`
2. `ductor_bot/orchestrator/core.py` (`_start_api_server` wiring)
3. `ductor_bot/files/*` (path safety, MIME detection, upload prompt construction)

If automation is not firing:

1. cron: `ductor_bot/cron/observer.py`
2. webhooks: `ductor_bot/webhook/server.py`, `ductor_bot/webhook/observer.py`
3. heartbeat: `ductor_bot/heartbeat/observer.py`
4. quiet-hour logic: `ductor_bot/utils/quiet_hours.py`
5. dependency locking: `ductor_bot/cron/dependency_queue.py`

If rules/skills drift:

1. `ductor_bot/workspace/init.py`
2. `ductor_bot/workspace/rules_selector.py`
3. `ductor_bot/workspace/skill_sync.py`

## 7) Behavior details to remember

- `/stop` is middleware-level abort handling before normal command routing.
- `/new` resets only the active provider bucket in that chat.
- cron/webhook `cron_task` runs support provider/model/reasoning/CLI-arg overrides.
- direct API upload writes to `workspace/api_files/YYYY-MM-DD/`.
- rule sync updates existing `CLAUDE.md`, `AGENTS.md`, and `GEMINI.md` siblings by mtime.
- Zone 2 overwrite in workspace init includes:
  - `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`
  - `workspace/tools/cron_tools/*.py`
  - `workspace/tools/webhook_tools/*.py`

Continue with `docs/architecture.md` and `docs/modules/*.md` for subsystem details.
