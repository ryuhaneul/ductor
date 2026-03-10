"""Interactive onboarding wizard for first-time setup."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ductor_bot.cli.auth import AuthStatus, check_claude_auth, check_codex_auth, check_gemini_auth
from ductor_bot.config import DEFAULT_EMPTY_GEMINI_API_KEY, AgentConfig, deep_merge_config
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import resolve_paths

_BANNER_PATH = Path(__file__).resolve().parent.parent / "_banner.txt"
logger = logging.getLogger(__name__)


def _load_banner() -> str:
    """Read ASCII art from bundled file."""
    try:
        return _BANNER_PATH.read_text(encoding="utf-8").rstrip()
    except OSError:
        return "ductor.dev"


_TOKEN_PATTERN = re.compile(r"^\d{8,}:[A-Za-z0-9_-]{30,}$")
_MATRIX_USER_RE = re.compile(r"^@[a-z0-9._=/+-]+:[a-z0-9.-]+$", re.IGNORECASE)

_TIMEZONES: list[str] = [
    # Europe
    "Europe/Berlin",
    "Europe/London",
    "Europe/Paris",
    "Europe/Zurich",
    "Europe/Moscow",
    "Europe/Amsterdam",
    "Europe/Rome",
    "Europe/Madrid",
    # Americas
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "America/Toronto",
    # Asia & Middle East
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Asia/Singapore",
    # Oceania & Other
    "Australia/Sydney",
    "Pacific/Auckland",
    "UTC",
]

_MANUAL_TZ_OPTION = "-> Enter manually"


def _abort() -> NoReturn:
    """Print abort message and exit."""
    Console().print("\n[dim]Setup cancelled.[/dim]\n")
    sys.exit(0)


def _show_banner(console: Console) -> None:
    """Display the ASCII art banner."""
    banner = Text(_load_banner(), style="bold cyan")
    console.print(
        Panel(
            banner,
            subtitle="[dim]ductor.dev[/dim]",
            border_style="cyan",
            padding=(0, 2),
        ),
    )


_STATUS_ICON = {
    AuthStatus.AUTHENTICATED: "[bold green]authenticated[/bold green]",
    AuthStatus.INSTALLED: "[bold yellow]installed but not logged in[/bold yellow]",
    AuthStatus.NOT_FOUND: "[dim]not found[/dim]",
}


def _check_clis(console: Console) -> None:
    """Detect CLI availability and require at least one authenticated provider."""
    claude = check_claude_auth()
    codex = check_codex_auth()
    gemini = check_gemini_auth()

    lines = [
        "[bold]Detected AI Backends:[/bold]\n",
        f"  Claude Code CLI   {_STATUS_ICON[claude.status]}",
        f"  OpenAI Codex CLI  {_STATUS_ICON[codex.status]}",
        f"  Google Gemini CLI {_STATUS_ICON[gemini.status]}",
    ]

    has_auth = claude.is_authenticated or codex.is_authenticated or gemini.is_authenticated

    if has_auth:
        border = "green"
    else:
        border = "red"
        lines.append(
            "\n[bold red]At least one CLI must be installed and authenticated.[/bold red]\n\n"
            "  Claude: [dim]https://docs.anthropic.com/en/docs/claude-code[/dim]\n"
            "  Codex:  [dim]https://github.com/openai/codex[/dim]\n"
            "  Gemini: [dim]https://github.com/google-gemini/gemini-cli[/dim]"
        )

    console.print(
        Panel(
            "\n".join(lines),
            title="[bold]CLI Backends[/bold]",
            border_style=border,
            padding=(1, 2),
        ),
    )

    if not has_auth:
        console.print()
        _abort()


def _show_disclaimer(console: Console) -> None:
    """Display the risk disclaimer and require confirmation."""
    disclaimer = (
        "[bold]Important -- please read before continuing.[/bold]\n\n"
        "ductor connects to [bold]Anthropic Claude CLI[/bold] and "
        "[bold]OpenAI Codex CLI[/bold] as AI agent backends.\n\n"
        "The bot operates in [bold yellow]full permission bypass mode[/bold yellow]. "
        "The agent can read, write, and delete files, execute commands, "
        "and interact with your system without asking for confirmation.\n\n"
        "While safeguards are in place, [bold red]unintended actions can occur[/bold red] "
        "-- including data loss, unexpected file changes, or unintended command execution.\n\n"
        "[bold green]We strongly recommend running ductor inside a Docker container[/bold green] "
        "to isolate it from your host system."
    )
    console.print(
        Panel(
            disclaimer,
            title="[bold yellow]Disclaimer[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )

    accepted = questionary.confirm(
        "I understand the risks and want to continue.",
        default=False,
    ).ask()
    if not accepted:
        _abort()


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------


def _ask_transport(console: Console) -> str:
    """Prompt for the messaging transport (Telegram or Matrix)."""
    console.print(
        Panel(
            "[bold]Choose how users will talk to the bot:[/bold]\n\n"
            "  [bold cyan]Telegram[/bold cyan]  — Requires a bot token from @BotFather\n"
            "  [bold cyan]Matrix[/bold cyan]    — Requires a Matrix account on a homeserver (e.g. Element)",
            title="[bold]Messaging Transport[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    selected: str | None = questionary.select(
        "Select transport:",
        choices=["Telegram", "Matrix"],
    ).ask()
    if selected is None:
        _abort()
    return "matrix" if selected == "Matrix" else "telegram"


# ---------------------------------------------------------------------------
# Telegram setup
# ---------------------------------------------------------------------------


def _ask_telegram_token(console: Console) -> str:
    """Prompt for the Telegram bot token with instructions."""
    instructions = (
        "[bold]How to get your Telegram Bot Token:[/bold]\n\n"
        "  1. Open Telegram and search for [bold cyan]@BotFather[/bold cyan]\n"
        "  2. Send [bold]/newbot[/bold] and follow the prompts\n"
        "  3. Choose a name and username for your bot\n"
        "  4. BotFather will reply with your bot token\n\n"
        "[dim]Token format: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz[/dim]"
    )
    console.print(
        Panel(
            instructions,
            title="[bold]Telegram Bot Token[/bold]",
            border_style="blue",
            padding=(1, 2),
        )
    )

    while True:
        token: str | None = questionary.text("Paste your bot token:").ask()
        if token is None:
            _abort()
        token = token.strip()
        if _TOKEN_PATTERN.match(token):
            return str(token)
        console.print(
            "[red]Invalid token format. Expected: digits:alphanumeric (e.g. 123456:ABC-xyz)[/red]"
        )


def _ask_user_id(console: Console) -> list[int]:
    """Prompt for the Telegram user ID with instructions."""
    instructions = (
        "[bold]How to find your Telegram User ID:[/bold]\n\n"
        "  1. Open Telegram and search for [bold cyan]@userinfobot[/bold cyan]\n"
        "  2. Send [bold]/start[/bold] to the bot\n"
        "  3. It will reply with your numeric user ID\n\n"
        "[dim]Only messages from this user ID will be accepted by the bot.[/dim]"
    )
    console.print(
        Panel(
            instructions, title="[bold]Telegram User ID[/bold]", border_style="blue", padding=(1, 2)
        )
    )

    while True:
        raw = questionary.text("Enter your numeric user ID:").ask()
        if raw is None:
            _abort()
        raw = raw.strip()
        try:
            uid = int(raw)
        except ValueError:
            console.print("[red]Please enter a valid number.[/red]")
            continue
        if uid <= 0:
            console.print("[red]User ID must be a positive number.[/red]")
            continue
        return [uid]


# ---------------------------------------------------------------------------
# Matrix setup
# ---------------------------------------------------------------------------


def _ask_matrix_homeserver(console: Console) -> str:
    """Prompt for the Matrix homeserver URL."""
    console.print(
        Panel(
            "[bold]Enter your Matrix homeserver URL.[/bold]\n\n"
            "  This is the server where your bot account lives.\n\n"
            "[dim]Example: https://matrix.example.com[/dim]",
            title="[bold]Matrix Homeserver[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    while True:
        url: str | None = questionary.text("Homeserver URL:").ask()
        if url is None:
            _abort()
        url = url.strip().rstrip("/")
        if url.startswith("https://") and len(url) > len("https://"):
            return url
        console.print("[red]Must be an HTTPS URL (e.g. https://matrix.example.com)[/red]")


def _ask_matrix_user_id(console: Console) -> str:
    """Prompt for the Matrix bot user ID."""
    console.print(
        Panel(
            "[bold]Enter the bot's Matrix user ID.[/bold]\n\n"
            "  Create a dedicated account for the bot on your homeserver.\n\n"
            "[dim]Format: @botname:homeserver.domain[/dim]",
            title="[bold]Matrix Bot User ID[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    while True:
        uid: str | None = questionary.text("Bot user ID:").ask()
        if uid is None:
            _abort()
        uid = uid.strip()
        if _MATRIX_USER_RE.match(uid):
            return uid
        console.print(
            "[red]Invalid format. Expected: @localpart:domain (e.g. @mybot:matrix.org)[/red]"
        )


def _ask_matrix_password(console: Console) -> str:
    """Prompt for the Matrix account password."""
    console.print(
        Panel(
            "[bold]Enter the bot account's password.[/bold]\n\n"
            "  Used for the initial login only. After first login, an access\n"
            "  token is saved and the password is no longer needed.",
            title="[bold]Matrix Password[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    while True:
        pw: str | None = questionary.password("Password:").ask()
        if pw is None:
            _abort()
        pw = pw.strip()
        if pw:
            return pw
        console.print("[red]Password cannot be empty.[/red]")


def _ask_matrix_allowed_users(console: Console) -> list[str]:
    """Prompt for allowed Matrix user IDs."""
    console.print(
        Panel(
            "[bold]Who should be allowed to talk to this bot?[/bold]\n\n"
            "  Enter your Matrix user ID. Only messages from allowed users\n"
            "  will be processed.\n\n"
            "[dim]Format: @username:homeserver.domain[/dim]",
            title="[bold]Allowed Users[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    while True:
        raw: str | None = questionary.text("Your Matrix user ID:").ask()
        if raw is None:
            _abort()
        raw = raw.strip()
        if _MATRIX_USER_RE.match(raw):
            return [raw]
        console.print("[red]Invalid format. Expected: @user:domain (e.g. @nik:matrix.org)[/red]")


# ---------------------------------------------------------------------------
# Common steps
# ---------------------------------------------------------------------------


def _ask_docker(console: Console) -> bool:
    """Detect Docker and ask whether to enable sandboxing."""
    docker_found = shutil.which("docker") is not None

    if docker_found:
        console.print(
            Panel(
                "[bold green]Docker detected on your system.[/bold green]\n\n"
                "Running ductor inside Docker isolates it from your host.\n"
                "This is the recommended setup for safety.",
                title="[bold]Docker Sandboxing[/bold]",
                border_style="green",
                padding=(1, 2),
            ),
        )
        enabled: bool | None = questionary.confirm(
            "Enable Docker sandboxing? (Recommended)",
            default=True,
        ).ask()
        if enabled is None:
            _abort()
        return bool(enabled)

    console.print(
        Panel(
            "[bold yellow]Docker was not found on your system.[/bold yellow]\n\n"
            "We recommend installing Docker to run the bot in an isolated container.\n"
            "You can enable Docker sandboxing later in the config.\n\n"
            "[dim]https://docs.docker.com/get-docker/[/dim]",
            title="[bold]Docker Sandboxing[/bold]",
            border_style="yellow",
            padding=(1, 2),
        ),
    )
    return False


def _build_extras_table(console: Console) -> None:
    """Print a Rich overview table of all available Docker extras."""
    from rich.table import Table

    from ductor_bot.infra.docker_extras import DOCKER_EXTRAS_BY_ID, extras_for_display

    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 2),
        title="[bold]Available Docker Extras[/bold]",
        title_style="bold blue",
    )
    table.add_column("Package", style="bold green", min_width=18)
    table.add_column("What it does", min_width=40)
    table.add_column("Size", style="cyan", justify="right")

    for category, extras in extras_for_display():
        table.add_row(f"[bold yellow]{category}[/bold yellow]", "", "")
        for extra in extras:
            dep_hint = ""
            if extra.depends_on:
                dep_names = ", ".join(
                    DOCKER_EXTRAS_BY_ID[d].name
                    for d in extra.depends_on
                    if d in DOCKER_EXTRAS_BY_ID
                )
                if dep_names:
                    dep_hint = f" [dim](+ {dep_names})[/dim]"
            table.add_row(
                f"  {extra.name}",
                f"{extra.description}{dep_hint}",
                extra.size_estimate,
            )

    console.print()
    console.print(table)
    console.print()
    console.print(
        "[dim]These packages are optional and increase image build time.\n"
        "You can change this later with"
        " [cyan]ductor docker extras-add / extras-remove[/cyan].[/dim]"
    )
    console.print()


def _ask_docker_extras(console: Console) -> list[str]:
    """Prompt for optional Docker sandbox packages."""
    from ductor_bot.infra.docker_extras import (
        DOCKER_EXTRAS_BY_ID,
        extras_for_display,
        resolve_extras,
    )

    _build_extras_table(console)

    # -- checkbox selection ---------------------------------------------------
    choices: list[questionary.Choice | questionary.Separator] = []
    for category, extras in extras_for_display():
        choices.append(questionary.Separator(f"── {category} ──"))
        choices.extend(
            questionary.Choice(
                title=f"{extra.name}  ({extra.size_estimate})",
                value=extra.id,
            )
            for extra in extras
        )

    selected: list[str] | None = questionary.checkbox(
        "Select extras (Space to toggle, Enter to confirm):",
        choices=choices,
    ).ask()

    if selected is None:
        _abort()

    if not selected:
        return []

    # -- resolve dependencies -------------------------------------------------
    resolved = resolve_extras(selected)
    resolved_ids = [e.id for e in resolved]

    added_deps = set(resolved_ids) - set(selected)
    if added_deps:
        dep_names = ", ".join(
            DOCKER_EXTRAS_BY_ID[d].name for d in added_deps if d in DOCKER_EXTRAS_BY_ID
        )
        if dep_names:
            console.print(f"[dim]Auto-added dependencies: {dep_names}[/dim]")

    return resolved_ids


def _ask_timezone(console: Console) -> str:
    """Prompt for timezone selection."""
    console.print(
        Panel(
            "Your timezone is used for cron scheduling, heartbeat quiet hours,\n"
            "and daily session resets.",
            title="[bold]Timezone[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    choices = [*_TIMEZONES, _MANUAL_TZ_OPTION]
    selected: str | None = questionary.select("Select your timezone:", choices=choices).ask()
    if selected is None:
        _abort()

    if selected != _MANUAL_TZ_OPTION:
        return str(selected)

    while True:
        manual: str | None = questionary.text("Enter IANA timezone (e.g. Europe/Berlin):").ask()
        if manual is None:
            _abort()
        manual = manual.strip()
        try:
            ZoneInfo(manual)
        except (ZoneInfoNotFoundError, KeyError):
            console.print(f"[red]Unknown timezone: {manual}[/red]")
            continue
        return str(manual)


def _offer_service_install(console: Console) -> bool:
    """Ask whether to install ductor as a background service."""
    from ductor_bot.infra.service import is_service_available

    if not is_service_available():
        return False

    is_windows = sys.platform == "win32"
    is_macos = sys.platform == "darwin"
    if is_windows:
        mechanism = "scheduled task"
        trigger = "login"
    elif is_macos:
        mechanism = "launch agent"
        trigger = "login"
    else:
        mechanism = "systemd service"
        trigger = "boot"

    console.print(
        Panel(
            f"[bold]Run ductor as a background service?[/bold]\n\n"
            f"This creates a {mechanism} that:\n\n"
            f"  - Starts ductor on {trigger}\n"
            "  - Restarts automatically on crash\n"
            "  - Keeps running in the background\n\n"
            "[dim]Recommended for VPS or always-on setups.[/dim]",
            title="[bold]Background Service[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    enabled: bool | None = questionary.confirm(
        "Install as background service? (Recommended for VPS)",
        default=True,
    ).ask()
    if enabled is None:
        _abort()
    console.print()
    return bool(enabled)


# ---------------------------------------------------------------------------
# Config writing
# ---------------------------------------------------------------------------


class _WizardConfig(TypedDict, total=False):
    """Wizard values passed to ``_write_config``."""

    transport: str
    user_timezone: str
    docker_enabled: bool
    docker_extras: list[str] | None
    # Telegram
    telegram_token: str
    allowed_user_ids: list[int] | None
    # Matrix
    matrix_homeserver: str
    matrix_user_id: str
    matrix_password: str
    matrix_allowed_users: list[str] | None


def _load_existing_config(config_path: Path) -> dict[str, object]:
    """Load existing config or return empty dict."""
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Ignoring invalid config file during onboarding: %s",
                config_path,
            )
    return {}


def _apply_transport_config(merged: dict[str, object], cfg: _WizardConfig) -> None:
    """Write transport-specific keys into *merged*."""
    if cfg.get("transport", "telegram") == "telegram":
        merged["telegram_token"] = cfg.get("telegram_token", "")
        merged["allowed_user_ids"] = cfg.get("allowed_user_ids") or []
    else:  # matrix
        matrix_section = merged.get("matrix")
        if not isinstance(matrix_section, dict):
            matrix_section = {}
            merged["matrix"] = matrix_section
        matrix_section["homeserver"] = cfg.get("matrix_homeserver", "")
        matrix_section["user_id"] = cfg.get("matrix_user_id", "")
        matrix_section["password"] = cfg.get("matrix_password", "")
        matrix_section["allowed_users"] = cfg.get("matrix_allowed_users") or []
        matrix_section["store_path"] = "matrix_store"


def _write_config(cfg: _WizardConfig) -> Path:
    """Write the config file with wizard values merged into defaults."""
    docker_enabled = cfg.get("docker_enabled", False)

    paths = resolve_paths()
    config_path = paths.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _load_existing_config(config_path)

    defaults = AgentConfig().model_dump(mode="json")
    defaults["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY
    merged, _ = deep_merge_config(existing, defaults)
    if merged.get("gemini_api_key") is None:
        merged["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY

    merged["transport"] = cfg.get("transport", "telegram")
    merged["user_timezone"] = cfg.get("user_timezone", "UTC")
    raw_docker = merged.get("docker")
    if isinstance(raw_docker, dict):
        docker_section = raw_docker
    else:
        docker_section = {"enabled": docker_enabled}
        merged["docker"] = docker_section
    docker_section["enabled"] = docker_enabled
    docker_extras = cfg.get("docker_extras")
    if docker_extras is not None:
        docker_section["extras"] = docker_extras

    _apply_transport_config(merged, cfg)

    from ductor_bot.infra.json_store import atomic_json_save

    atomic_json_save(config_path, merged)

    init_workspace(paths)
    return config_path


# ---------------------------------------------------------------------------
# Onboarding flow
# ---------------------------------------------------------------------------


def run_onboarding() -> bool:
    """Run onboarding and return True only when service install succeeded."""
    console = Console()
    console.print()
    _show_banner(console)

    _check_clis(console)
    console.print()

    _show_disclaimer(console)
    console.print()

    transport = _ask_transport(console)
    console.print()

    # Transport-specific credentials
    telegram_token = ""
    allowed_user_ids: list[int] = []
    matrix_homeserver = ""
    matrix_user_id = ""
    matrix_password = ""
    matrix_allowed_users: list[str] = []

    if transport == "telegram":
        telegram_token = _ask_telegram_token(console)
        console.print()
        allowed_user_ids = _ask_user_id(console)
        console.print()
    else:  # matrix
        matrix_homeserver = _ask_matrix_homeserver(console)
        console.print()
        matrix_user_id = _ask_matrix_user_id(console)
        console.print()
        matrix_password = _ask_matrix_password(console)
        console.print()
        matrix_allowed_users = _ask_matrix_allowed_users(console)
        console.print()

    docker_enabled = _ask_docker(console)
    console.print()

    docker_extras: list[str] = []
    if docker_enabled:
        docker_extras = _ask_docker_extras(console)
        console.print()

    timezone = _ask_timezone(console)
    console.print()

    config_path = _write_config(
        _WizardConfig(
            transport=transport,
            user_timezone=timezone,
            docker_enabled=docker_enabled,
            docker_extras=docker_extras,
            telegram_token=telegram_token,
            allowed_user_ids=allowed_user_ids,
            matrix_homeserver=matrix_homeserver,
            matrix_user_id=matrix_user_id,
            matrix_password=matrix_password,
            matrix_allowed_users=matrix_allowed_users,
        )
    )

    paths = resolve_paths()

    # Offer background service setup on Linux with systemd
    run_as_service = _offer_service_install(console)

    console.print(
        Panel(
            "[bold green]Setup complete![/bold green]\n\n"
            "[bold]Your ductor files:[/bold]\n\n"
            f"  Home:       [cyan]{paths.ductor_home}[/cyan]\n"
            f"  Config:     [cyan]{config_path}[/cyan]\n"
            f"  Workspace:  [cyan]{paths.workspace}[/cyan]\n"
            f"  Logs:       [cyan]{paths.logs_dir}[/cyan]\n\n"
            + ("Installing service..." if run_as_service else "Starting bot..."),
            title="[bold green]Ready[/bold green]",
            border_style="green",
            padding=(1, 2),
        ),
    )
    console.print()

    service_installed = False
    if run_as_service:
        from ductor_bot.infra.service import install_service

        service_installed = install_service(console)

    return service_installed


def run_smart_reset(ductor_home: Path) -> None:
    """Read existing config, handle Docker cleanup, and delete workspace."""
    console = Console()
    console.print()

    config_path = ductor_home / "config" / "config.json"

    # Read Docker config from existing setup
    docker_container: str | None = None
    docker_image: str | None = None
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            docker = data.get("docker", {})
            if isinstance(docker, dict) and docker.get("enabled"):
                docker_container = str(docker.get("container_name", "ductor-sandbox"))
                docker_image = str(docker.get("image_name", "ductor-sandbox"))
        except (json.JSONDecodeError, OSError):
            pass

    # Warning panel
    console.print(
        Panel(
            "[bold yellow]You already have a configured setup.[/bold yellow]\n\n"
            "Re-running onboarding will perform a [bold red]full reset[/bold red]:\n\n"
            f"  [dim]{ductor_home}[/dim] will be deleted entirely.\n"
            "  All sessions, configs, memory, and cron tasks will be lost.",
            title="[bold yellow]Existing Setup Detected[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        ),
    )

    # Docker cleanup offer
    if docker_container and shutil.which("docker"):
        console.print()
        console.print(
            Panel(
                "[bold]Docker sandboxing is enabled in your current config.[/bold]\n\n"
                f"  Container: [cyan]{docker_container}[/cyan]\n"
                f"  Image:     [cyan]{docker_image}[/cyan]",
                title="[bold]Docker Cleanup[/bold]",
                border_style="blue",
                padding=(1, 2),
            ),
        )
        remove_docker: bool | None = questionary.confirm(
            "Remove the Docker container and image?",
            default=True,
        ).ask()
        if remove_docker is None:
            _abort()
        if remove_docker:
            console.print("[dim]Removing Docker resources...[/dim]")
            subprocess.run(
                ["docker", "stop", "-t", "5", docker_container],
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["docker", "rm", "-f", docker_container],
                capture_output=True,
                check=False,
            )
            if docker_image:
                subprocess.run(
                    ["docker", "rmi", docker_image],
                    capture_output=True,
                    check=False,
                )
            console.print("[green]Docker cleanup done.[/green]")

    # Final confirmation
    console.print()
    confirmed: bool | None = questionary.confirm(
        "Delete everything and start fresh?",
        default=False,
    ).ask()
    if not confirmed:
        _abort()

    from ductor_bot.infra.fs import robust_rmtree

    robust_rmtree(ductor_home)
    if ductor_home.exists():
        console.print(
            f"[yellow]Warning: Could not fully delete {ductor_home}. Remove manually.[/yellow]\n"
        )
    else:
        console.print("[dim]Workspace deleted.[/dim]\n")
