# Setup Wizard and CLI Entry

Covers `ductor` CLI command behavior, onboarding wizard, and upgrade/restart/uninstall flows.

## Files

- `ductor_bot/__main__.py`: CLI command dispatch + process lifecycle
- `ductor_bot/cli/init_wizard.py`: onboarding wizard + smart reset
- `ductor_bot/infra/service*.py`: background service backends
- `ductor_bot/infra/version.py`, `infra/updater.py`: version check + upgrade helpers
- `ductor_bot/orchestrator/commands.py`: Telegram `/upgrade` command

## CLI commands

- `ductor`: start bot (auto-onboarding if not configured)
- `ductor onboarding` / `ductor reset`: run onboarding (smart reset first if configured)
- `ductor status`: show status panel
- `ductor stop`: stop bot + docker container (if enabled)
- `ductor restart`: stop and re-exec
- `ductor upgrade`: CLI-side upgrade + restart (non-dev installs)
- `ductor uninstall`: full removal workflow
- `ductor service <install|status|start|stop|logs|uninstall>`: service control
- `ductor docker <rebuild|enable|disable>`: Docker lifecycle + config toggle
- `ductor help`: command table + status hint

Command resolution in `main()` takes the first matching non-flag command token.

## Onboarding flow (`run_onboarding`)

1. banner
2. provider detection (`claude`, `codex`, `gemini`) and auth status panel
3. require at least one authenticated provider
4. disclaimer confirmation
5. Telegram bot token prompt + validation
6. Telegram user ID prompt + validation
7. Docker detection + opt-in prompt
8. timezone prompt + validation
9. write merged config and run `init_workspace`
10. optional service install prompt

Return semantics:

- returns `True` only when service install was requested and installation succeeded
- returns `False` otherwise

Caller behavior:

- both default start path and onboarding/reset path call `_stop_bot()` first to avoid duplicate runtime instances,
- `ductor` default path: exits after successful service install; otherwise starts foreground bot
- `ductor onboarding` / `ductor reset`: same behavior (no forced foreground start after successful service install)

## Smart reset (`run_smart_reset`)

If already configured and onboarding/reset is requested:

1. read current Docker settings
2. show destructive reset warning
3. optionally remove Docker container/image
4. require final confirmation
5. delete `~/.ductor`

## Configured check

`_is_configured()` requires:

- valid non-placeholder `telegram_token`
- non-empty `allowed_user_ids`

## Status panel (`ductor status`)

Shows:

- running state / PID / uptime
- configured provider + model
- Docker enabled state
- error count from newest `ductor*.log`
- key paths (home/config/workspace/logs/sessions)

Note: runtime file logger writes `agent.log`; status counter still scans `ductor*.log`.

## Service command wiring

`ductor service ...` delegates to platform backend via `infra/service.py`:

- Linux: systemd user service
- macOS: launchd Launch Agent
- Windows: Task Scheduler

Windows-specific behavior:

- prefers `pythonw.exe -m ductor_bot` for windowless background execution,
- falls back to `ductor` binary when `pythonw.exe` is unavailable,
- installs Task Scheduler restart-on-failure retries (3 attempts, 1-minute interval),
- shows an explicit admin-help panel on `schtasks` access-denied errors.

`ductor service logs` behavior:

- Linux: live journalctl stream
- macOS/Windows: recent lines from `agent.log` (fallback newest `*.log`)

## Telegram upgrade flow

`/upgrade` command path:

1. check PyPI version
2. if update available, send inline buttons
3. callback `upg:yes:<version>` runs upgrade pipeline with verification + one automatic forced retry when needed
4. on confirmed version change: write sentinel and exit with restart code
5. startup consumes sentinel and sends completion message

`UpdateObserver` runs in bot startup only for upgradeable installs (`pipx`/`pip`, not dev mode).
