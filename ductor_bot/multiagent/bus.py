"""InterAgentBus: in-memory async message passing between agents."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.multiagent.stack import AgentStack

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0  # 5 minutes
_MAX_LOG_SIZE = 100  # Keep last N messages in log


@dataclass(slots=True)
class InterAgentMessage:
    """A message sent between agents."""

    sender: str
    recipient: str
    message: str
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class InterAgentResponse:
    """Response from an inter-agent message."""

    sender: str
    text: str
    success: bool = True
    error: str | None = None


class InterAgentBus:
    """In-memory async bus for agent-to-agent communication.

    All agents in the same process share this bus. Messages are handled
    by calling the target agent's Orchestrator.handle_interagent_message().
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentStack] = {}
        self._message_log: list[InterAgentMessage] = []

    def register(self, name: str, stack: AgentStack) -> None:
        """Register an agent on the bus."""
        self._agents[name] = stack
        logger.debug("Bus: registered agent '%s'", name)

    def unregister(self, name: str) -> None:
        """Unregister an agent from the bus."""
        self._agents.pop(name, None)
        logger.debug("Bus: unregistered agent '%s'", name)

    def list_agents(self) -> list[str]:
        """List all registered agent names."""
        return list(self._agents.keys())

    async def send(
        self,
        sender: str,
        recipient: str,
        message: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> InterAgentResponse:
        """Send a message to another agent and wait for the response.

        The target agent's Orchestrator runs a one-shot CLI turn to process
        the message. Returns the response text or an error.
        """
        if recipient not in self._agents:
            available = ", ".join(self._agents.keys()) or "(none)"
            return InterAgentResponse(
                sender=recipient,
                text="",
                success=False,
                error=f"Agent '{recipient}' not found. Available: {available}",
            )

        target = self._agents[recipient]
        msg = InterAgentMessage(sender=sender, recipient=recipient, message=message)
        self._message_log.append(msg)

        # Trim log to prevent unbounded growth
        if len(self._message_log) > _MAX_LOG_SIZE:
            self._message_log = self._message_log[-_MAX_LOG_SIZE:]

        logger.info("Bus: %s -> %s (%d chars)", sender, recipient, len(message))

        try:
            orch = target.bot._orchestrator
            if orch is None:
                return InterAgentResponse(
                    sender=recipient,
                    text="",
                    success=False,
                    error=f"Agent '{recipient}' orchestrator not initialized",
                )

            result = await asyncio.wait_for(
                orch.handle_interagent_message(sender, message),
                timeout=timeout,
            )
            logger.info(
                "Bus: %s -> %s completed (%d chars response)",
                sender, recipient, len(result),
            )
            return InterAgentResponse(sender=recipient, text=result)

        except asyncio.TimeoutError:
            logger.warning("Bus: %s -> %s timed out after %.0fs", sender, recipient, timeout)
            return InterAgentResponse(
                sender=recipient,
                text="",
                success=False,
                error=f"Timeout after {timeout:.0f}s",
            )
        except Exception as exc:
            logger.exception("Bus: %s -> %s failed", sender, recipient)
            return InterAgentResponse(
                sender=recipient,
                text="",
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
