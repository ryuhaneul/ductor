"""Internal localhost HTTP API bridging CLI subprocesses to the InterAgentBus.

CLI subprocesses (claude, codex, gemini) run as separate OS processes and
cannot access the in-memory bus directly. This lightweight aiohttp server
exposes ``/interagent/send`` and ``/interagent/agents`` on localhost only,
so tool scripts like ``ask_agent.py`` can communicate via the bus.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import InterAgentBus

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8799


class InternalAgentAPI:
    """Localhost-only HTTP server for CLI → Bus communication."""

    def __init__(self, bus: InterAgentBus, port: int = _DEFAULT_PORT) -> None:
        self._bus = bus
        self._port = port
        self._app = web.Application()
        self._app.router.add_post("/interagent/send", self._handle_send)
        self._app.router.add_get("/interagent/agents", self._handle_list)
        self._runner: web.AppRunner | None = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        """Start the internal API server on 127.0.0.1."""
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        try:
            site = web.TCPSite(self._runner, "127.0.0.1", self._port)
            await site.start()
            logger.info("Internal agent API listening on 127.0.0.1:%d", self._port)
        except OSError:
            logger.exception(
                "Failed to start internal agent API on port %d", self._port,
            )

    async def stop(self) -> None:
        """Stop the internal API server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Internal agent API stopped")

    async def _handle_send(self, request: web.Request) -> web.Response:
        """POST /interagent/send — send a message to another agent.

        Expects JSON body: ``{"from": "agent_name", "to": "agent_name", "message": "..."}``
        Returns JSON: ``{"sender": "...", "text": "...", "success": true/false, "error": "..."}``
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "Invalid JSON body"},
                status=400,
            )

        sender = data.get("from", "")
        recipient = data.get("to", "")
        message = data.get("message", "")

        if not recipient or not message:
            return web.json_response(
                {"success": False, "error": "Missing 'to' or 'message' field"},
                status=400,
            )

        result = await self._bus.send(
            sender=sender,
            recipient=recipient,
            message=message,
        )
        return web.json_response(asdict(result))

    async def _handle_list(self, request: web.Request) -> web.Response:
        """GET /interagent/agents — list all registered agents."""
        return web.json_response({"agents": self._bus.list_agents()})
