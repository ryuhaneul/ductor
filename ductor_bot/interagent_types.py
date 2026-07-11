"""Dependency-neutral inter-agent data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class InterAgentOrigin:
    """Originating transport context for a sender inter-agent request."""

    transport: str
    chat_id: int
    topic_id: int | None = None

    def valid(self) -> bool:
        """Return True when the origin can scope an inter-agent named session."""
        return self.transport in {"tg", "mx"} and bool(self.chat_id)


@dataclass(slots=True)
class InterAgentOutcome:
    """Typed result from recipient inter-agent handling."""

    text: str
    session_name: str
    notice: str
    ok: bool
    error_kind: Literal["busy", "ceiling", "execution", "cli"] | None = None


class IARunningLimiter:
    """Small non-blocking recipient-wide limiter for IA CLI executions."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._running = 0

    def try_acquire(self) -> bool:
        if self._running >= self._limit:
            return False
        self._running += 1
        return True

    def release(self) -> None:
        if self._running > 0:
            self._running -= 1
