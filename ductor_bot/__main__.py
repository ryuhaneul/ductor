"""Entry point: python -m ductor_bot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ductor_bot.config import DEFAULT_EMPTY_GEMINI_API_KEY, AgentConfig, deep_merge_config
from ductor_bot.infra.restart import EXIT_RESTART
from ductor_bot.logging_config import setup_logging
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths

logger = logging.getLogger(__name__)

_console = Console()

_IS_WINDOWS = sys.platform == "win32"

_RMTREE_RETRIES = 3
_RMTREE_RETRY_DELAY = 1.0


def _robust_rmtree(path: Path) -> None:
    """Remove a directory tree, handling locked files on Windows.

    On Windows, processes can hold file locks (e.g. log files). This helper:
    1. Clears read-only flags on permission errors
    2. Retries the full rmtree up to ``_RMTREE_RETRIES`` times with a delay
    """

    def _on_error(
        func: Callable[..., object],
        fpath: str,
        _exc_info: object,
    ) -> None:
        """Handle permission errors by clearing read-only and retrying."""
        try:
            Path(fpath).chmod(stat.S_IWRITE | stat.S_IREAD)
            func(fpath)
        except OSError:
            pass

    last_exc: Exception | None = None
    for attempt in range(_RMTREE_RETRIES):
        try:
            shutil.rmtree(path, onerror=_on_error)
        except OSError as exc:
            last_exc = exc
        else:
            return

        if attempt < _RMTREE_RETRIES - 1:
            logger.debug(
                "rmtree attempt %d failed for %s, retrying in %.0fs",
                attempt + 1,
                path,
                _RMTREE_RETRY_DELAY,
            )
            time.sleep(_RMTREE_RETRY_DELAY)

    if last_exc:
        logger.warning("Could not fully remove %s: %s", path, last_exc)


def _re_exec_bot() -> NoReturn:
    """Re-exec the bot process (cross-platform).

    On POSIX: replaces current process via ``os.execv`` (same PID, same cgroup).
    On Windows: spawns new process and exits (``os.execv`` doesn't truly replace).
    """
    args = [sys.executable, "-m", "ductor_bot"]
    if _IS_WINDOWS:
        subprocess.Popen(args)
        sys.exit(0)
    else:
        os.execv(sys.executable, args)  # noqa: S606


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _is_configured() -> bool:
    """Check if bot has a valid configuration."""
    paths = resolve_paths()
    if not paths.config_path.exists():
        return False
    try:
        data = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    token = data.get("telegram_token", "")
    users = data.get("allowed_user_ids", [])
    return bool(token) and not str(token).startswith("YOUR_") and bool(users)


def load_config() -> AgentConfig:
    """Load, auto-create, and smart-merge the bot config.

    Resolution order:
    1. ``~/.ductor/config/config.json`` (canonical location)
    2. Copy from ``config.example.json`` in the framework root on first start
    3. Fall back to Pydantic defaults if example file is missing

    On every load the config is deep-merged with current Pydantic defaults
    so that new fields from framework updates are added without destroying
    user settings.
    """
    paths = resolve_paths()
    config_path = paths.config_path

    first_start = not config_path.exists()

    if first_start:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        example = paths.config_example_path
        if example.is_file():
            shutil.copy2(example, config_path)
            logger.info("Created config from config.example.json at %s", config_path)
        else:
            defaults = AgentConfig().model_dump(mode="json")
            defaults["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY
            defaults.pop("api", None)  # Beta: only written by `ductor api enable`
            config_path.write_text(
                json.dumps(defaults, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("Created default config at %s", config_path)

    try:
        user_data: dict[str, object] = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to parse config at %s", config_path)
        sys.exit(1)

    normalized_existing = False
    if user_data.get("gemini_api_key") is None:
        user_data["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY
        normalized_existing = True

    defaults = AgentConfig().model_dump(mode="json")
    defaults["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY
    defaults.pop("api", None)  # Beta: only written by `ductor api enable`
    merged, changed = deep_merge_config(user_data, defaults)
    changed = changed or normalized_existing

    if changed:
        config_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("Extended config with new default fields")

    init_workspace(paths)
    return AgentConfig.model_validate(merged)


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------


async def run_telegram(config: AgentConfig) -> int:
    """Validate config and run the bot via AgentSupervisor.

    The supervisor manages the main agent and dynamically created sub-agents
    from ``agents.json``.  If no sub-agents are defined, the supervisor runs
    only the main agent — behaviour is identical to the old single-bot path.

    Returns the exit code from the bot (``0`` = clean, ``42`` = restart requested).
    """
    paths = resolve_paths(ductor_home=config.ductor_home)

    missing_token = not config.telegram_token or config.telegram_token.startswith("YOUR_")
    if missing_token or not config.allowed_user_ids:
        _console.print(
            "[bold yellow]Config is incomplete. Run [bold]ductor onboarding[/bold].[/bold yellow]"
        )
        sys.exit(1)

    from ductor_bot.infra.pidlock import acquire_lock, release_lock
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    acquire_lock(pid_file=paths.ductor_home / "bot.pid", kill_existing=True)

    supervisor = AgentSupervisor(config)
    exit_code = 0
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    installed_signals: list[signal.Signals] = []

    def _request_shutdown() -> None:
        if current_task is not None and not current_task.done():
            current_task.cancel()

    if current_task is not None and sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_shutdown)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            installed_signals.append(sig)

    try:
        exit_code = await supervisor.start()
    except asyncio.CancelledError:
        logger.info("Termination signal received, shutting down gracefully...")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        await supervisor.stop_all()
        release_lock(pid_file=paths.ductor_home / "bot.pid")
    return exit_code


def _start_bot(verbose: bool = False) -> None:
    """Load config and start the Telegram bot."""
    paths = resolve_paths()
    setup_logging(verbose=verbose, log_dir=paths.logs_dir)
    config = load_config()
    if not verbose:
        config_level = getattr(logging, config.log_level.upper(), logging.INFO)
        if config_level != logging.INFO:
            setup_logging(level=config_level, log_dir=paths.logs_dir)
    try:
        exit_code = asyncio.run(run_telegram(config))
    except KeyboardInterrupt:
        exit_code = 0
    if exit_code == EXIT_RESTART:
        if os.environ.get("DUCTOR_SUPERVISOR") or os.environ.get("INVOCATION_ID"):
            sys.exit(EXIT_RESTART)
        _re_exec_bot()
    elif exit_code:
        sys.exit(exit_code)


def _stop_bot() -> None:
    """Stop all running ductor instances and Docker container.

    1. Stop the system service (prevents Task Scheduler/systemd/launchd respawn)
    2. Kill the PID-file instance
    3. Kill any remaining ductor processes system-wide
    4. Wait for file locks to release (Windows only)
    5. Stop Docker container if enabled
    """
    from ductor_bot.infra.pidlock import _is_process_alive, _kill_and_wait

    # 1. Stop service to prevent respawn
    _stop_service_if_running()

    # 2. Kill PID-file instance
    paths = resolve_paths()
    pid_file = paths.ductor_home / "bot.pid"
    stopped = False

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None and _is_process_alive(pid):
            _console.print(f"[dim]Stopping bot (pid={pid})...[/dim]")
            _kill_and_wait(pid)
            pid_file.unlink(missing_ok=True)
            _console.print("[green]Bot stopped.[/green]")
            stopped = True
        else:
            pid_file.unlink(missing_ok=True)

    # 3. Kill all remaining ductor processes system-wide
    from ductor_bot.infra.process_tree import kill_all_ductor_processes

    extra = kill_all_ductor_processes()
    if extra:
        _console.print(f"[dim]Killed {extra} remaining ductor process(es).[/dim]")
        stopped = True

    if not stopped:
        _console.print("[dim]No running bot instance found.[/dim]")

    # 4. Brief wait for file locks to release on Windows
    if _IS_WINDOWS and stopped:
        time.sleep(1.0)

    # 5. Stop Docker container if enabled in config
    config_path = paths.config_path
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            docker = data.get("docker", {})
            if isinstance(docker, dict) and docker.get("enabled"):
                container = str(docker.get("container_name", "ductor-sandbox"))
                _stop_docker_container(container)
        except (json.JSONDecodeError, OSError):
            pass


def _stop_service_if_running() -> None:
    """Stop the system service if installed and running."""
    import contextlib

    with contextlib.suppress(Exception):
        from ductor_bot.infra.service import is_service_installed, is_service_running, stop_service

        if is_service_installed() and is_service_running():
            stop_service(_console)


def _stop_docker_container(container_name: str) -> None:
    """Stop and remove a Docker container."""
    if not shutil.which("docker"):
        return
    _console.print(f"[dim]Stopping Docker container '{container_name}'...[/dim]")
    subprocess.run(
        ["docker", "stop", "-t", "5", container_name],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        check=False,
    )
    _console.print("[green]Docker container stopped.[/green]")


# ---------------------------------------------------------------------------
# Help & Status
# ---------------------------------------------------------------------------


def _print_usage() -> None:
    """Print commands and smart status information."""
    _console.print()
    banner_path = Path(__file__).resolve().parent / "_banner.txt"
    try:
        banner_text = banner_path.read_text(encoding="utf-8").rstrip()
    except OSError:
        banner_text = "ductor.dev"
    _console.print(
        Panel(
            Text(banner_text, style="bold cyan"),
            subtitle="[dim]ductor.dev[/dim]",
            border_style="cyan",
            padding=(0, 2),
        ),
    )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=24)
    table.add_column()
    table.add_row("ductor", "Start the bot (runs onboarding if needed)")
    table.add_row("ductor onboarding", "Setup wizard (resets if already configured)")
    table.add_row("ductor stop", "Stop running bot and Docker container")
    table.add_row("ductor restart", "Restart the bot")
    table.add_row("ductor reset", "Full reset and re-setup")
    table.add_row("ductor upgrade", "Stop, upgrade to latest, restart")
    table.add_row("ductor uninstall", "Remove everything and uninstall")
    is_macos = sys.platform == "darwin"
    svc_hint = "Task Scheduler" if _IS_WINDOWS else ("launchd" if is_macos else "systemd")
    table.add_row("ductor service install", f"Run as background service ({svc_hint})")
    table.add_row("ductor service", "Service management (status/stop/logs/...)")
    table.add_row("ductor docker", "Docker management (rebuild/enable/disable)")
    table.add_row("ductor api", "API server management (enable/disable) [beta]")
    table.add_row("ductor status", "Show bot status, paths, and stats")
    table.add_row("ductor help", "Show this message")
    table.add_row("-v, --verbose", "Verbose logging output")

    _console.print(
        Panel(table, title="[bold]Commands[/bold]", border_style="blue", padding=(1, 0)),
    )

    if _is_configured():
        _print_status()
    else:
        _console.print(
            Panel(
                "[bold yellow]Not configured.[/bold yellow]\n\n"
                "Run [bold]ductor[/bold] to start the setup wizard.",
                title="[bold]Status[/bold]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
    _console.print()


@dataclass(slots=True)
class _StatusSummary:
    """Runtime status inputs needed by the status panel renderer."""

    bot_running: bool
    bot_pid: int | None
    bot_uptime: str
    provider: str
    model: str
    docker_enabled: bool
    docker_name: str | None
    error_count: int


def _build_status_lines(status: _StatusSummary, *, paths: DuctorPaths) -> list[str]:
    """Assemble the status panel content lines."""
    lines: list[str] = []
    if status.bot_running:
        lines.append(
            f"[bold green]Running[/bold green]  pid={status.bot_pid}  uptime: {status.bot_uptime}"
        )
    else:
        lines.append("[dim]Not running[/dim]")
    lines.append(f"Provider:  [cyan]{status.provider}[/cyan] ({status.model})")
    if status.docker_enabled:
        lines.append(f"Docker:    [green]enabled[/green] ({status.docker_name})")
    else:
        lines.append("Docker:    [dim]disabled[/dim]")
    if status.error_count > 0:
        lines.append(f"Errors:    [bold red]{status.error_count}[/bold red] in latest log")
    else:
        lines.append("Errors:    [green]0[/green]")
    lines.append("")
    lines.append("[bold]Paths:[/bold]")
    lines.append(f"  Home:       [cyan]{paths.ductor_home}[/cyan]")
    lines.append(f"  Config:     [cyan]{paths.config_path}[/cyan]")
    lines.append(f"  Workspace:  [cyan]{paths.workspace}[/cyan]")
    lines.append(f"  Logs:       [cyan]{paths.logs_dir}[/cyan]")
    lines.append(f"  Sessions:   [cyan]{paths.sessions_path}[/cyan]")
    return lines


def _print_status() -> None:
    """Print bot status, paths, and runtime info."""
    paths = resolve_paths()
    try:
        data: dict[str, object] = json.loads(
            paths.config_path.read_text(encoding="utf-8"),
        )
    except (json.JSONDecodeError, OSError):
        return

    provider = data.get("provider", "claude")
    model = data.get("model", "opus")
    docker_cfg = data.get("docker", {})
    docker_enabled = isinstance(docker_cfg, dict) and bool(docker_cfg.get("enabled"))
    docker_name: str | None = None
    if docker_enabled and isinstance(docker_cfg, dict):
        docker_name = str(docker_cfg.get("container_name", "ductor-sandbox"))

    # Running state
    pid_file = paths.ductor_home / "bot.pid"
    bot_running = False
    bot_pid: int | None = None
    bot_uptime = ""
    if pid_file.exists():
        try:
            bot_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            bot_pid = None
        if bot_pid is not None:
            from ductor_bot.infra.pidlock import _is_process_alive

            bot_running = _is_process_alive(bot_pid)
            if bot_running:
                mtime = datetime.fromtimestamp(pid_file.stat().st_mtime, tz=UTC)
                delta = datetime.now(UTC) - mtime
                hours, remainder = divmod(int(delta.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                bot_uptime = f"{hours}h {minutes}m"

    # Error count from latest log
    error_count = _count_log_errors(paths.logs_dir)

    # Build status lines
    summary = _StatusSummary(
        bot_running=bot_running,
        bot_pid=bot_pid,
        bot_uptime=bot_uptime,
        provider=str(provider),
        model=str(model),
        docker_enabled=docker_enabled,
        docker_name=str(docker_name) if docker_name else None,
        error_count=error_count,
    )
    lines = _build_status_lines(summary, paths=paths)

    _console.print(
        Panel(
            "\n".join(lines),
            title="[bold]Status[/bold]",
            border_style="green",
            padding=(1, 2),
        ),
    )


def _count_log_errors(log_dir: Path) -> int:
    """Count ERROR entries in the most recent log file."""
    if not log_dir.is_dir():
        return 0
    log_files = sorted(
        log_dir.glob("ductor*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not log_files:
        return 0
    try:
        return log_files[0].read_text(encoding="utf-8", errors="replace").count(" ERROR ")
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def _uninstall() -> None:
    """Full uninstall: stop bot, remove Docker, delete workspace, uninstall package."""
    import questionary

    _console.print()
    _console.print(
        Panel(
            "[bold red]This will permanently remove ductor from your system.[/bold red]\n\n"
            "  1. Stop the running bot (if active)\n"
            "  2. Remove Docker container and image (if used)\n"
            "  3. Delete all data in ~/.ductor/\n"
            "  4. Uninstall the ductor package",
            title="[bold red]Uninstall ductor[/bold red]",
            border_style="red",
            padding=(1, 2),
        ),
    )

    confirmed: bool | None = questionary.confirm(
        "Are you sure you want to uninstall everything?",
        default=False,
    ).ask()
    if not confirmed:
        _console.print("\n[dim]Uninstall cancelled.[/dim]\n")
        return

    # 1. Stop bot + Docker container + all ductor processes
    _stop_bot()

    # 2. Remove Docker image
    paths = resolve_paths()
    if paths.config_path.exists():
        try:
            data = json.loads(paths.config_path.read_text(encoding="utf-8"))
            docker = data.get("docker", {})
            if isinstance(docker, dict) and docker.get("enabled") and shutil.which("docker"):
                image = str(docker.get("image_name", "ductor-sandbox"))
                _console.print(f"[dim]Removing Docker image '{image}'...[/dim]")
                subprocess.run(
                    ["docker", "rmi", image],
                    capture_output=True,
                    check=False,
                )
                _console.print("[green]Docker image removed.[/green]")
        except (json.JSONDecodeError, OSError):
            pass

    # 3. Delete workspace
    ductor_home = paths.ductor_home
    if ductor_home.exists():
        _robust_rmtree(ductor_home)
        if ductor_home.exists():
            _console.print(
                f"[yellow]Warning: Could not fully delete {ductor_home} "
                "(some files may be locked). Remove manually.[/yellow]"
            )
        else:
            _console.print(f"[green]Deleted {ductor_home}[/green]")

    # 4. Uninstall package
    _console.print("[dim]Uninstalling ductor package...[/dim]")
    if shutil.which("pipx"):
        subprocess.run(
            ["pipx", "uninstall", "ductor"],
            capture_output=True,
            check=False,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "ductor"],
            capture_output=True,
            check=False,
        )

    _console.print(
        Panel(
            "[bold green]ductor has been completely removed.[/bold green]\n\n"
            "Thank you for using ductor!",
            title="[bold green]Uninstalled[/bold green]",
            border_style="green",
            padding=(1, 2),
        ),
    )
    _console.print()


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def _upgrade() -> None:
    """Stop bot, upgrade package, restart."""
    from ductor_bot.infra.install import detect_install_mode
    from ductor_bot.infra.updater import perform_upgrade_pipeline
    from ductor_bot.infra.version import get_current_version

    mode = detect_install_mode()
    if mode == "dev":
        _console.print(
            Panel(
                "[bold yellow]Running from source (editable install).[/bold yellow]\n\n"
                "Self-upgrade is not available.\n"
                "Update with [bold]git pull[/bold] in your project directory.",
                title="[bold]Upgrade[/bold]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
        return

    _console.print()
    _console.print(
        Panel(
            "[bold cyan]Upgrading ductor...[/bold cyan]\n\n"
            "  1. Stop running bot gracefully\n"
            "  2. Upgrade to latest version\n"
            "  3. Restart",
            title="[bold]Upgrade[/bold]",
            border_style="cyan",
            padding=(1, 2),
        ),
    )

    current = get_current_version()

    # 1. Graceful stop
    _stop_bot()

    # 2. Upgrade + verification pipeline
    _console.print("[dim]Upgrading package...[/dim]")
    changed, actual, output = asyncio.run(
        perform_upgrade_pipeline(current_version=current),
    )
    if output:
        _console.print(f"[dim]{output}[/dim]")

    if not changed:
        _console.print(
            f"[bold yellow]Version unchanged after upgrade ({actual}).[/bold yellow]\n"
            "Automatic retry was attempted, but no new installed version could be verified yet."
        )
        return

    _console.print(f"[green]Upgrade complete: {current} -> {actual}[/green]")

    # 3. Re-exec with new version
    _console.print("[dim]Restarting...[/dim]")
    _re_exec_bot()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _cmd_status() -> None:
    """Show bot status or hint to configure."""
    _console.print()
    if _is_configured():
        _print_status()
    else:
        _console.print(
            Panel(
                "[bold yellow]Not configured.[/bold yellow]\n\n"
                "Run [bold]ductor[/bold] to start the setup wizard.",
                title="[bold]Status[/bold]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
    _console.print()


def _cmd_restart() -> None:
    """Stop and re-exec the bot."""
    _stop_bot()
    _re_exec_bot()


def _cmd_setup(verbose: bool) -> None:
    """Run onboarding (with smart reset if already configured), then start."""
    from ductor_bot.cli.init_wizard import run_onboarding, run_smart_reset

    _stop_bot()
    paths = resolve_paths()
    if _is_configured():
        run_smart_reset(paths.ductor_home)
    service_installed = run_onboarding()
    if service_installed:
        return
    _start_bot(verbose)


_COMMANDS: dict[str, str] = {
    "help": "help",
    "status": "status",
    "stop": "stop",
    "restart": "restart",
    "upgrade": "upgrade",
    "uninstall": "uninstall",
    "onboarding": "setup",
    "reset": "setup",
    "service": "service",
    "docker": "docker",
    "api": "api",
}

_SERVICE_SUBCOMMANDS = frozenset({"install", "status", "stop", "start", "logs", "uninstall"})
_Action = Callable[[], None]


def _parse_service_subcommand(args: list[str]) -> str | None:
    """Extract the subcommand after 'service' from CLI args."""
    found_service = False
    for a in args:
        if a.startswith("-"):
            continue
        if not found_service and a == "service":
            found_service = True
            continue
        if found_service:
            return a if a in _SERVICE_SUBCOMMANDS else None
    return None


def _print_service_help() -> None:
    """Print the service subcommand help table."""
    _console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=30)
    table.add_column()
    table.add_row("ductor service install", "Install and start background service")
    table.add_row("ductor service status", "Show service status")
    table.add_row("ductor service start", "Start the service")
    table.add_row("ductor service stop", "Stop the service")
    table.add_row("ductor service logs", "View live logs")
    table.add_row("ductor service uninstall", "Remove the service")
    _console.print(
        Panel(table, title="[bold]Service Commands[/bold]", border_style="blue", padding=(1, 0)),
    )
    _console.print()


def _cmd_service(args: list[str]) -> None:
    """Handle 'ductor service <subcommand>'."""
    from ductor_bot.infra.service import (
        install_service,
        print_service_logs,
        print_service_status,
        start_service,
        stop_service,
        uninstall_service,
    )

    sub = _parse_service_subcommand(args)
    if sub is None:
        _print_service_help()
        return

    def _install() -> None:
        install_service(_console)

    def _status() -> None:
        print_service_status(_console)

    def _start() -> None:
        start_service(_console)

    def _stop() -> None:
        stop_service(_console)

    def _logs() -> None:
        print_service_logs(_console)

    def _uninstall_service_cmd() -> None:
        uninstall_service(_console)

    dispatch: dict[str, _Action] = {
        "install": _install,
        "status": _status,
        "start": _start,
        "stop": _stop,
        "logs": _logs,
        "uninstall": _uninstall_service_cmd,
    }
    _console.print()
    dispatch[sub]()
    _console.print()


# ---------------------------------------------------------------------------
# Docker management
# ---------------------------------------------------------------------------

_DOCKER_SUBCOMMANDS = frozenset({"rebuild", "enable", "disable", "mount", "unmount", "mounts"})


def _parse_docker_subcommand(args: list[str]) -> str | None:
    """Extract the subcommand after 'docker' from CLI args."""
    found = False
    for a in args:
        if a.startswith("-"):
            continue
        if not found and a == "docker":
            found = True
            continue
        if found:
            return a if a in _DOCKER_SUBCOMMANDS else None
    return None


def _parse_docker_mount_arg(args: list[str]) -> str | None:
    """Extract the path argument after 'docker mount/unmount' from CLI args.

    Expects the form: ``ductor docker mount <path>`` where *args* is
    ``sys.argv[1:]`` (no ``ductor`` prefix).  Non-flag positionals are
    ``docker`` (1), ``mount``/``unmount`` (2), ``<path>`` (3).
    """
    positionals = [a for a in args if not a.startswith("-")]
    # positionals: ['docker', 'mount', '<path>']
    return positionals[2] if len(positionals) >= 3 else None


def _print_docker_help() -> None:
    """Print the docker subcommand help table."""
    _console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=36)
    table.add_column()
    table.add_row("ductor docker rebuild", "Remove container & image, rebuild on next start")
    table.add_row("ductor docker enable", "Enable Docker sandboxing")
    table.add_row("ductor docker disable", "Disable Docker sandboxing")
    table.add_row("ductor docker mount <path>", "Mount a host directory into the sandbox")
    table.add_row("ductor docker unmount <path>", "Remove a mounted directory")
    table.add_row("ductor docker mounts", "List all mounted directories")
    _console.print(
        Panel(table, title="[bold]Docker Commands[/bold]", border_style="blue", padding=(1, 0)),
    )
    _console.print()


def _docker_read_config() -> tuple[Path, dict[str, object]] | None:
    """Read config.json and return (path, data) or None."""
    paths = resolve_paths()
    config_path = paths.config_path
    if not config_path.exists():
        _console.print("[bold red]Config not found. Run ductor first.[/bold red]")
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _console.print("[bold red]Failed to read config.[/bold red]")
        return None
    return config_path, data


def _docker_set_enabled(*, enabled: bool) -> None:
    """Set docker.enabled in config.json and handle running state."""
    result = _docker_read_config()
    if result is None:
        return
    config_path, data = result

    docker = data.setdefault("docker", {})
    if not isinstance(docker, dict):
        data["docker"] = docker = {}
    docker["enabled"] = enabled
    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    if not enabled:
        container = str(docker.get("container_name", "ductor-sandbox"))
        _stop_docker_container(container)

    state = "[green]enabled[/green]" if enabled else "[dim]disabled[/dim]"
    _console.print(f"Docker sandboxing: {state}")
    _console.print("[dim]Restart the bot to apply.[/dim]")


def _docker_rebuild() -> None:
    """Stop bot, remove container and image, so they get rebuilt on restart."""
    if not shutil.which("docker"):
        _console.print("[bold red]Docker not found.[/bold red]")
        return

    result = _docker_read_config()
    container = "ductor-sandbox"
    image = "ductor-sandbox"
    if result is not None:
        _, data = result
        docker = data.get("docker", {})
        if isinstance(docker, dict):
            container = str(docker.get("container_name", container))
            image = str(docker.get("image_name", image))

    _console.print("[dim]Stopping bot...[/dim]")
    _stop_bot()

    _console.print(f"[dim]Removing container '{container}'...[/dim]")
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)

    _console.print(f"[dim]Removing image '{image}'...[/dim]")
    subprocess.run(["docker", "rmi", image], capture_output=True, check=False)

    _console.print(
        "[green]Done.[/green] Image will be rebuilt on next bot start.\n"
        "[dim]If running as a service, it will restart automatically.[/dim]"
    )


def _expand_path(raw: str) -> Path:
    """Expand env vars and ``~`` in a path string."""
    return Path(os.path.expandvars(raw)).expanduser()


def _docker_get_mounts(data: dict[str, object]) -> list[object]:
    """Return the ``docker.mounts`` list from config, ensuring it exists."""
    docker = data.setdefault("docker", {})
    if not isinstance(docker, dict):
        data["docker"] = docker = {}
    raw = docker.get("mounts")
    if not isinstance(raw, list):
        raw = []
        docker["mounts"] = raw
    return raw


def _is_duplicate_mount(mounts: list[object], resolved_str: str) -> bool:
    """Return True if *resolved_str* already exists in the mount list."""
    for existing in mounts:
        if not isinstance(existing, str):
            continue
        try:
            if str(_expand_path(existing).resolve()) == resolved_str:
                return True
        except OSError:
            continue
    return False


def _docker_mount(args: list[str]) -> None:
    """Add a host directory to the Docker sandbox mounts."""
    raw_path = _parse_docker_mount_arg(args)
    if not raw_path:
        _console.print("[bold red]Usage: ductor docker mount <path>[/bold red]")
        return

    expanded = _expand_path(raw_path)
    try:
        resolved = expanded.resolve(strict=True)
    except OSError:
        _console.print(f"[bold red]Path does not exist: {raw_path}[/bold red]")
        return
    if not resolved.is_dir():
        _console.print(f"[bold red]Not a directory: {raw_path}[/bold red]")
        return

    result = _docker_read_config()
    if result is None:
        return
    config_path, data = result
    mounts = _docker_get_mounts(data)
    resolved_str = str(resolved)

    if _is_duplicate_mount(mounts, resolved_str):
        _console.print(f"[dim]Already mounted: {resolved}[/dim]")
        return

    mounts.append(resolved_str)
    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    from ductor_bot.infra.docker import resolve_mount_target

    pair = resolve_mount_target(resolved_str, set())
    target_info = f" -> [cyan]{pair[1]}[/cyan]" if pair else ""
    _console.print(f"[green]Mounted:[/green] {resolved}{target_info}")
    _console.print("[dim]Restart the bot (or rebuild the container) to apply.[/dim]")


def _find_mount_entry(mounts: list[object], raw_path: str) -> str | None:
    """Find a matching entry in the mounts list by exact, resolved, or basename match."""
    expanded = _expand_path(raw_path)
    try:
        resolved_str = str(expanded.resolve())
    except OSError:
        resolved_str = str(expanded)
    query_basename = expanded.name

    for entry in mounts:
        if not isinstance(entry, str):
            continue
        if entry in (raw_path, resolved_str):
            return entry
        try:
            if str(_expand_path(entry).resolve()) == resolved_str:
                return entry
        except OSError:
            pass
        if Path(entry).name == query_basename:
            return entry
    return None


def _docker_unmount(args: list[str]) -> None:
    """Remove a host directory from the Docker sandbox mounts."""
    raw_path = _parse_docker_mount_arg(args)
    if not raw_path:
        _console.print("[bold red]Usage: ductor docker unmount <path>[/bold red]")
        return

    result = _docker_read_config()
    if result is None:
        return
    config_path, data = result

    docker = data.get("docker", {})
    if not isinstance(docker, dict) or not isinstance(docker.get("mounts"), list):
        _console.print("[dim]No mounts configured.[/dim]")
        return
    mounts: list[object] = docker["mounts"]

    to_remove = _find_mount_entry(mounts, raw_path)
    if to_remove is None:
        _console.print(f"[bold red]Mount not found: {raw_path}[/bold red]")
        return

    mounts.remove(to_remove)
    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _console.print(f"[green]Unmounted:[/green] {to_remove}")
    _console.print("[dim]Restart the bot (or rebuild the container) to apply.[/dim]")


def _docker_list_mounts() -> None:
    """List all configured Docker sandbox mounts."""
    result = _docker_read_config()
    if result is None:
        return
    _, data = result

    docker = data.get("docker", {})
    mounts = docker.get("mounts", []) if isinstance(docker, dict) else []
    if not isinstance(mounts, list) or not mounts:
        _console.print("[dim]No mounts configured.[/dim]")
        _console.print("[dim]Use 'ductor docker mount <path>' to add one.[/dim]")
        return

    from ductor_bot.infra.docker import resolve_mount_target

    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Host Path", style="bold")
    table.add_column("Container Path", style="cyan")
    table.add_column("Status")

    used_names: set[str] = set()
    for entry in mounts:
        if not isinstance(entry, str):
            continue
        pair = resolve_mount_target(entry, used_names)
        if pair is not None:
            host_resolved, container_target = pair
            table.add_row(str(host_resolved), container_target, "[green]OK[/green]")
        else:
            table.add_row(entry, "-", "[red]not found[/red]")

    _console.print(table)


def _cmd_docker(args: list[str]) -> None:
    """Handle 'ductor docker <subcommand>'."""
    sub = _parse_docker_subcommand(args)
    if sub is None:
        _print_docker_help()
        return

    dispatch: dict[str, _Action] = {
        "rebuild": _docker_rebuild,
        "enable": lambda: _docker_set_enabled(enabled=True),
        "disable": lambda: _docker_set_enabled(enabled=False),
        "mount": lambda: _docker_mount(args),
        "unmount": lambda: _docker_unmount(args),
        "mounts": _docker_list_mounts,
    }
    _console.print()
    dispatch[sub]()
    _console.print()


# ---------------------------------------------------------------------------
# API management (beta)
# ---------------------------------------------------------------------------

_API_SUBCOMMANDS = frozenset({"enable", "disable"})


def _parse_api_subcommand(args: list[str]) -> str | None:
    """Extract the subcommand after 'api' from CLI args."""
    found = False
    for a in args:
        if a.startswith("-"):
            continue
        if not found and a == "api":
            found = True
            continue
        if found:
            return a if a in _API_SUBCOMMANDS else None
    return None


def _print_api_help() -> None:
    """Print the API subcommand help table with current status."""
    _console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=30)
    table.add_column()
    table.add_row("ductor api enable", "Enable the WebSocket API server")
    table.add_row("ductor api disable", "Disable the WebSocket API server")

    # Show current status
    paths = resolve_paths()
    status = "[dim]not configured[/dim]"
    if paths.config_path.exists():
        try:
            data = json.loads(paths.config_path.read_text(encoding="utf-8"))
            api_cfg = data.get("api", {})
            if isinstance(api_cfg, dict) and api_cfg.get("enabled"):
                port = api_cfg.get("port", 8741)
                status = f"[green]enabled[/green] (port {port})"
            elif isinstance(api_cfg, dict):
                status = "[dim]disabled[/dim]"
        except (json.JSONDecodeError, OSError):
            pass

    _console.print(
        Panel(
            table,
            title="[bold]API Commands[/bold] [dim](beta)[/dim]",
            border_style="blue",
            padding=(1, 0),
        ),
    )
    _console.print(f"  Status: {status}")
    _console.print()


def _nacl_available() -> bool:
    """Check if PyNaCl is importable."""
    try:
        import nacl.public  # noqa: F401
    except ImportError:
        return False
    else:
        return True


def _api_install_hint() -> str:
    """Return the install command for PyNaCl based on install mode."""
    from ductor_bot.infra.install import detect_install_mode

    mode = detect_install_mode()
    if mode == "pipx":
        return "pipx inject ductor PyNaCl"
    return "pip install ductor[api]"


def _api_enable() -> None:
    """Enable the API server: check deps, write config, generate token."""
    if not _nacl_available():
        hint = _api_install_hint()
        _console.print(
            Panel(
                "[bold yellow]PyNaCl is required for the API server (E2E encryption).[/bold yellow]"
                f"\n\nInstall it with:\n\n  [bold]{hint}[/bold]"
                "\n\nThen run [bold]ductor api enable[/bold] again.",
                title="[bold]Missing dependency[/bold]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
        return

    result = _docker_read_config()
    if result is None:
        return
    config_path, data = result

    import secrets as _secrets

    api = data.get("api", {})
    if not isinstance(api, dict):
        api = {}
    api["enabled"] = True
    if not api.get("token"):
        api["token"] = _secrets.token_urlsafe(32)
    api.setdefault("host", "0.0.0.0")  # noqa: S104
    api.setdefault("port", 8741)
    api.setdefault("chat_id", 0)
    api.setdefault("allow_public", False)
    data["api"] = api

    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    _console.print(
        Panel(
            "[bold green]API server enabled.[/bold green]\n\n"
            f"  Host:   [cyan]{api['host']}[/cyan]\n"
            f"  Port:   [cyan]{api['port']}[/cyan]\n"
            f"  Token:  [cyan]{api['token']}[/cyan]\n\n"
            "[dim]Restart the bot to start the API server.[/dim]\n"
            "[dim]Designed for use with Tailscale or other private networks.[/dim]",
            title="[bold]API Server[/bold] [dim](beta)[/dim]",
            border_style="green",
            padding=(1, 2),
        ),
    )


def _api_disable() -> None:
    """Disable the API server in config."""
    result = _docker_read_config()
    if result is None:
        return
    config_path, data = result

    api = data.get("api", {})
    if not isinstance(api, dict):
        api = {}
    api["enabled"] = False
    data["api"] = api

    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _console.print("API server: [dim]disabled[/dim]")
    _console.print("[dim]Restart the bot to apply.[/dim]")


def _cmd_api(args: list[str]) -> None:
    """Handle 'ductor api <subcommand>'."""
    sub = _parse_api_subcommand(args)
    if sub is None:
        _print_api_help()
        return

    dispatch: dict[str, _Action] = {
        "enable": _api_enable,
        "disable": _api_disable,
    }
    _console.print()
    dispatch[sub]()
    _console.print()


def _default_action(verbose: bool) -> None:
    """Auto-onboarding if unconfigured, then start bot."""
    if not _is_configured():
        from ductor_bot.cli.init_wizard import run_onboarding

        service_installed = run_onboarding()
        if service_installed:
            return
    _start_bot(verbose)


def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]
    commands = [a for a in args if not a.startswith("-")]
    verbose = "--verbose" in args or "-v" in args

    if "--help" in args or "-h" in args:
        commands.append("help")

    # Resolve first matching command
    action = next((_COMMANDS[c] for c in commands if c in _COMMANDS), None)

    dispatch: dict[str, _Action] = {
        "help": _print_usage,
        "status": _cmd_status,
        "stop": _stop_bot,
        "restart": _cmd_restart,
        "upgrade": _upgrade,
        "uninstall": _uninstall,
        "setup": lambda: _cmd_setup(verbose),
        "service": lambda: _cmd_service(args),
        "docker": lambda: _cmd_docker(args),
        "api": lambda: _cmd_api(args),
    }

    handler = dispatch.get(action) if action else None
    if handler is not None:
        handler()
    else:
        _default_action(verbose)


if __name__ == "__main__":
    main()
