# infra/

Runtime infrastructure: process lifecycle, restart/update flow, Docker sandbox, service backends.

## Files

- `pidlock.py`: single-instance PID lock
- `restart.py`: restart marker/sentinel helpers, `EXIT_RESTART = 42`
- `docker.py`: `DockerManager`
- `install.py`: install mode detection (`pipx` / `pip` / `dev`)
- `service.py`: platform dispatch facade
- `service_common.py`: shared console helper
- `service_logs.py`: shared recent-log renderer
- `service_linux.py`: Linux systemd backend
- `service_macos.py`: macOS launchd backend
- `service_windows.py`: Windows Task Scheduler backend
- `version.py`: PyPI version/changelog utilities
- `updater.py`: `UpdateObserver`, upgrade helpers/sentinel

Related runtime wrapper:

- `ductor_bot/run.py` (documented in [supervisor.md](supervisor.md)): optional supervisor with restart/backoff + hot-reload

## Service management

`service.py` dispatches by platform:

- Linux -> systemd user service (`service_linux.py`)
- macOS -> launchd Launch Agent (`service_macos.py`)
- Windows -> Task Scheduler (`service_windows.py`)

Shared helpers:

- `ensure_console()` in `service_common.py`
- `print_recent_logs()` in `service_logs.py`

`print_recent_logs()` behavior:

- prefers `~/.ductor/logs/agent.log`
- fallback: newest `*.log`
- prints last 50 lines by default

### Linux backend

- service file: `~/.config/systemd/user/ductor.service`
- optional linger enable via `sudo loginctl enable-linger <user>`
- logs command uses `journalctl --user -u ductor -f`

### macOS backend

- plist: `~/Library/LaunchAgents/dev.ductor.plist`
- launchd logs configured to `service.log` / `service.err`
- `ductor service logs` uses `print_recent_logs()` over ductor log files

### Windows backend

- scheduled task name: `ductor`
- starts 10s after logon
- restart-on-failure policy: up to 3 retries, 1 minute interval
- prefers `pythonw.exe -m ductor_bot`, fallback `ductor` binary
- explicit admin hint panel on access-denied `schtasks` errors
- `ductor service logs` uses `print_recent_logs()`

## PID lock

`acquire_lock(pid_file, kill_existing=True)` is used for bot startup.

- detects stale/alive PID
- optionally terminates existing process
- writes current PID

Windows compatibility includes broader `OSError` handling around PID liveness/termination checks.

## Restart protocol

- `/restart` or restart marker file triggers exit code `42`
- restart sentinel stores chat + message for post-restart notification
- sentinel consumed on next startup

## Docker manager

`DockerManager.setup()`:

1. verify Docker binary/daemon
2. ensure image (build when missing and `auto_build=true`)
3. reuse running container or start new one
4. mount `~/.ductor -> /ductor`
5. mount provider homes when present:
   - `~/.claude`
   - `~/.codex`
   - `~/.gemini`
   - `~/.claude.json` (file mount)
6. optionally mount host cache (`mount_host_cache` config flag):
   - Linux: `~/.cache` (or `$XDG_CACHE_HOME`)
   - macOS: `~/Library/Caches`
   - Windows: `%LOCALAPPDATA%`

Linux adds UID/GID mapping (`--user uid:gid`) to avoid root-owned host files.

If setup fails, orchestrator falls back to host execution.
At runtime, `Orchestrator._ensure_docker()` also health-checks the container and falls back to host execution if recovery fails.

The `Dockerfile.sandbox` includes Chrome/Chromium runtime dependencies (libgbm, libnss3, libasound2, etc.) for browser-based skills using patchright/playwright.

### Docker CLI commands

| Command | Effect |
|---|---|
| `ductor docker` | Show docker subcommand help |
| `ductor docker rebuild` | Stop bot, remove container & image (rebuilt on next start) |
| `ductor docker enable` | Set `docker.enabled = true` in config |
| `ductor docker disable` | Stop container, set `docker.enabled = false` in config |

`ductor docker rebuild` is safe to run while the bot runs as a service -- it stops the bot process first, the active service backend (systemd/launchd/Task Scheduler) can restart it, and the image is rebuilt during startup.

## Version/update system

- `check_pypi()` fetches latest package metadata
- `UpdateObserver` checks periodically and notifies once per new version
- `check_pypi(fresh=True)` adds cache-busting and no-cache headers for manual `/upgrade` checks
- `perform_upgrade_pipeline()` runs generic upgrade, verifies installed version with short settle polling, and performs one forced retry when needed
- upgrade sentinel stores old/new version + chat for post-restart confirmation

## Supervisor

See [supervisor.md](supervisor.md) for the optional wrapper process (`ductor_bot/run.py`).
