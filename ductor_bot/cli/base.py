"""Base types and abstract interface for CLI backends."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.stream_events import StreamEvent
from ductor_bot.cli.types import CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.process_registry import ProcessRegistry

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# 0x08000000 on Windows prevents a console window from appearing.
# On non-Windows, 0 is the default and has no effect.
_CREATION_FLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


def _win_feed_stdin(process: asyncio.subprocess.Process, data: str) -> None:
    """Write prompt to stdin and close on Windows; no-op on POSIX."""
    if _IS_WINDOWS and process.stdin is not None:
        process.stdin.write(data.encode())
        process.stdin.close()


async def _feed_stdin_and_close(
    process: asyncio.subprocess.Process,
    data: str,
    *,
    windows_only: bool = False,
) -> None:
    """Write prompt to stdin and close the writer gracefully."""
    if windows_only and not _IS_WINDOWS:
        return

    writer = process.stdin
    if writer is None:
        return

    with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError, ValueError):
        writer.write(data.encode())
        drain_result = writer.drain()
        if inspect.isawaitable(drain_result):
            await drain_result

    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return
    with contextlib.suppress(
        BrokenPipeError,
        ConnectionResetError,
        RuntimeError,
        OSError,
        ValueError,
    ):
        closed_result = wait_closed()
        if inspect.isawaitable(closed_result):
            await closed_result


@dataclass(slots=True)
class CLIConfig:
    """Configuration for any CLI wrapper."""

    provider: str = "claude"
    working_dir: str | Path = "."
    model: str | None = None
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    max_turns: int | None = None
    max_budget_usd: float | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "bypassPermissions"
    docker_container: str = ""
    # Codex-specific fields (ignored by Claude provider):
    sandbox_mode: str = "read-only"
    images: list[str] = field(default_factory=list)
    instructions: str | None = None
    reasoning_effort: str = "medium"
    # Process tracking (shared across providers):
    process_registry: ProcessRegistry | None = None
    chat_id: int = 0
    process_label: str = "main"
    # Gemini-specific auth fallback:
    gemini_api_key: str | None = None
    # Extra CLI parameters (provider-specific):
    cli_parameters: list[str] = field(default_factory=list)
    # Multi-agent identification:
    agent_name: str = "main"
    interagent_port: int = 8799


def docker_wrap(
    cmd: list[str],
    config: CLIConfig,
    *,
    extra_env: dict[str, str] | None = None,
    interactive: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap a CLI command for Docker execution if a container is set.

    *interactive* adds ``-i`` to keep stdin open (required for providers
    that pipe the prompt via stdin, e.g. Gemini).

    *extra_env* vars are injected as ``-e`` flags into ``docker exec``
    (set **inside** the container, unlike ``env=`` on the host process).
    """
    if config.docker_container:
        logger.debug("docker_wrap container=%s", config.docker_container)
        stdin_flag: list[str] = ["-i"] if interactive else []
        env_flags: list[str] = [
            "-e", f"DUCTOR_CHAT_ID={config.chat_id}",
            "-e", f"DUCTOR_AGENT_NAME={config.agent_name}",
            "-e", f"DUCTOR_INTERAGENT_PORT={config.interagent_port}",
        ]
        if extra_env:
            for key, value in extra_env.items():
                env_flags += ["-e", f"{key}={value}"]
        return (
            ["docker", "exec", *stdin_flag, *env_flags, config.docker_container, *cmd],
            None,
        )
    return cmd, str(Path(config.working_dir).resolve())


class BaseCLI(ABC):
    """Abstract interface for CLI backends (Claude, Codex, etc.)."""

    @abstractmethod
    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> CLIResponse: ...

    @abstractmethod
    def send_streaming(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> AsyncGenerator[StreamEvent, None]: ...
