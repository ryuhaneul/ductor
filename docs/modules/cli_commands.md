# cli_commands/

CLI command implementation package extracted from `__main__.py`.

## Files

- `cli_commands/lifecycle.py`: `start_bot`, `stop_bot`, `cmd_restart`, `upgrade`, `uninstall`, `_re_exec_bot`
- `cli_commands/status.py`: `print_status`, `print_usage`
- `cli_commands/service.py`: `ductor service ...`
- `cli_commands/docker.py`: `ductor docker ...`
- `cli_commands/api_cmd.py`: `ductor api ...`
- `cli_commands/agents.py`: `ductor agents ...`
- `cli_commands/install.py`: `ductor install <extra>`

## Role in runtime

`ductor_bot/__main__.py` is now a thin entrypoint:

- argument parsing + command dispatch
- config helpers (`_is_configured`, `load_config`, `run_telegram`)
- imports/re-exports command handlers from `cli_commands/*`

This keeps lifecycle logic testable and prevents command monolith growth.

## Command groups

- lifecycle: `ductor`, `stop`, `restart`, `upgrade`, `uninstall`, onboarding/reset flow
- status/help: `ductor status`, `ductor help`
- service: install/start/stop/logs/uninstall wrapper for platform backends
- docker: enable/disable/rebuild/mount/unmount/mounts/extras/extras-add/extras-remove
- api: enable/disable direct WebSocket API block in config
- agents: list/add/remove sub-agent entries in `agents.json`
- install extras: `ductor install <extra>` for optional Python extras (`matrix`, `api`)

## Notable behavior details

- `stop_bot()` stops service first, then PID instance, then remaining ductor processes, then Docker container (if enabled).
- `start_bot()` calls `load_config()` and starts `AgentSupervisor` via `run_telegram()`.
- restart semantics:
  - service/supervisor context -> exit with code `42`
  - direct foreground context -> process re-exec
- `status.py` currently counts errors from latest `ductor*.log`; runtime primary log file is `~/.ductor/logs/agent.log`.

## Why this matters for docs

When documenting CLI behavior, reference `cli_commands/*` for command internals.
Use `__main__.py` as the dispatch map, not as the implementation source.
