"""AgentSupervisor: manages main agent + dynamic sub-agents in a single process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ductor_bot.config import AgentConfig
from ductor_bot.infra.file_watcher import FileWatcher
from ductor_bot.infra.restart import EXIT_RESTART
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config
from ductor_bot.multiagent.registry import AgentRegistry
from ductor_bot.multiagent.stack import AgentStack
from ductor_bot.workspace.paths import resolve_paths

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import InterAgentBus
    from ductor_bot.multiagent.internal_api import InternalAgentAPI

logger = logging.getLogger(__name__)

_MAX_RESTART_RETRIES = 5
_RESTART_BACKOFF_BASE = 5  # seconds, doubles each retry


class AgentSupervisor:
    """Manages the main agent and dynamically created sub-agents.

    Watches ``agents.json`` via FileWatcher and starts/stops sub-agents
    as entries are added or removed. Each agent runs as a supervised
    asyncio task with automatic crash recovery.
    """

    def __init__(self, main_config: AgentConfig) -> None:
        self._main_config = main_config
        self._main_paths = resolve_paths(ductor_home=main_config.ductor_home)
        self._agents_path = self._main_paths.ductor_home / "agents.json"
        self._registry = AgentRegistry(self._agents_path)
        self._stacks: dict[str, AgentStack] = {}
        self._tasks: dict[str, asyncio.Task[int]] = {}
        self._health: dict[str, AgentHealth] = {}
        self._watcher = FileWatcher(self._agents_path, self._on_agents_changed)
        self._running = False
        self._main_done: asyncio.Event = asyncio.Event()

        # Bus and internal API — created lazily in start()
        self._bus: InterAgentBus | None = None
        self._internal_api: InternalAgentAPI | None = None

    @property
    def stacks(self) -> dict[str, AgentStack]:
        return self._stacks

    @property
    def health(self) -> dict[str, AgentHealth]:
        return self._health

    @property
    def bus(self) -> InterAgentBus | None:
        return self._bus

    async def start(self) -> int:
        """Start main agent + all sub-agents. Blocks until main agent exits."""
        self._running = True

        # Initialize inter-agent bus
        from ductor_bot.multiagent.bus import InterAgentBus
        from ductor_bot.multiagent.internal_api import InternalAgentAPI

        self._bus = InterAgentBus()
        self._internal_api = InternalAgentAPI(self._bus)
        await self._internal_api.start()
        logger.info("InterAgentBus and internal API started")

        # 1. Start main agent
        main_stack = await AgentStack.create(
            "main", self._main_config, is_main=True,
        )
        self._stacks["main"] = main_stack
        self._health["main"] = AgentHealth(name="main")
        self._bus.register("main", main_stack)

        self._tasks["main"] = asyncio.create_task(
            self._supervised_run("main", main_stack),
            name="agent:main",
        )

        # 2. Load and start sub-agents from agents.json
        await self._sync_sub_agents()

        # 3. Start FileWatcher for agents.json
        await self._watcher.start()

        # 4. Wait for main agent to finish — it determines the exit code
        await self._main_done.wait()
        main_task = self._tasks.get("main")
        exit_code = 0
        if main_task and main_task.done():
            try:
                exit_code = main_task.result()
            except (asyncio.CancelledError, Exception):
                exit_code = 1

        return exit_code

    async def _supervised_run(self, name: str, stack: AgentStack) -> int:
        """Run an agent with automatic crash recovery.

        On crash: retry with exponential backoff (5s, 10s, 20s, 40s, 80s).
        After ``_MAX_RESTART_RETRIES`` consecutive failures, give up.
        On clean exit: return the exit code.
        On restart request (exit code 42): rebuild stack and restart.
        """
        health = self._health[name]
        health.mark_starting()
        retry_count = 0

        while self._running:
            try:
                # Register a post-startup hook to inject supervisor reference
                # into the orchestrator (which is created during _on_startup).
                self._inject_supervisor_hook(stack)

                health.mark_running()
                logger.info("Agent '%s' running", name)
                exit_code = await stack.run()

                if exit_code == EXIT_RESTART:
                    logger.info("Agent '%s' requested restart", name)
                    health.mark_starting()
                    await stack.shutdown()
                    stack = await self._rebuild_stack(name, stack)
                    retry_count = 0
                    continue

                # Clean exit
                logger.info("Agent '%s' exited cleanly (code=%d)", name, exit_code)
                health.mark_stopped()
                if name == "main":
                    self._main_done.set()
                return exit_code

            except asyncio.CancelledError:
                logger.info("Agent '%s' cancelled", name)
                health.mark_stopped()
                raise

            except Exception as exc:
                retry_count += 1
                error_msg = f"{type(exc).__name__}: {exc}"
                health.mark_crashed(error_msg)
                logger.error(
                    "Agent '%s' crashed (attempt %d/%d): %s",
                    name, retry_count, _MAX_RESTART_RETRIES, error_msg,
                    exc_info=True,
                )

                if name == "main":
                    # Main agent crash is fatal — exit the process
                    logger.error("Main agent crashed, terminating supervisor")
                    self._main_done.set()
                    return 1

                if retry_count > _MAX_RESTART_RETRIES:
                    logger.error(
                        "Agent '%s' exceeded max retries (%d), giving up",
                        name, _MAX_RESTART_RETRIES,
                    )
                    await self._notify_main_agent(
                        f"Sub-agent '{name}' stopped after {_MAX_RESTART_RETRIES} crashes: {error_msg}"
                    )
                    return 1

                wait = _RESTART_BACKOFF_BASE * (2 ** (retry_count - 1))
                logger.info("Agent '%s' restarting in %ds", name, wait)
                await asyncio.sleep(wait)

                try:
                    with contextlib.suppress(Exception):
                        await stack.shutdown()
                    stack = await self._rebuild_stack(name, stack)
                    health.mark_starting()
                except Exception:
                    logger.exception("Failed to rebuild agent '%s'", name)
                    continue

        health.mark_stopped()
        if name == "main":
            self._main_done.set()
        return 0

    def _inject_supervisor_hook(self, stack: AgentStack) -> None:
        """Register a dispatcher startup hook to inject supervisor reference.

        The orchestrator is created during TelegramBot._on_startup(). We register
        an additional startup handler that fires AFTER _on_startup and sets the
        supervisor reference + registers multi-agent commands on the main agent.
        """
        supervisor = self

        async def _post_startup() -> None:
            orch = stack.bot._orchestrator
            if orch is None:
                return
            orch._supervisor = supervisor
            if stack.is_main:
                orch.register_multiagent_commands()
            logger.debug("Supervisor reference injected into agent '%s'", stack.name)

        # aiogram runs startup handlers in registration order;
        # TelegramBot registers _on_startup in __init__, so ours runs after.
        stack.bot._dp.startup.register(_post_startup)

    async def _rebuild_stack(self, name: str, old_stack: AgentStack) -> AgentStack:
        """Rebuild an AgentStack from its config."""
        new_stack = await AgentStack.create(
            name, old_stack.config, is_main=old_stack.is_main,
        )
        self._stacks[name] = new_stack
        if self._bus:
            self._bus.register(name, new_stack)
        return new_stack

    # -- Sub-agent lifecycle ------------------------------------------------

    async def _sync_sub_agents(self) -> None:
        """Load agents.json and start any sub-agents not yet running."""
        sub_agents = self._registry.load()
        for sub_cfg in sub_agents:
            if sub_cfg.name not in self._stacks:
                await self._start_sub_agent(sub_cfg)

    async def _start_sub_agent(self, sub_cfg: SubAgentConfig) -> None:
        """Create and start a new sub-agent."""
        name = sub_cfg.name
        if name == "main":
            logger.warning("Cannot create sub-agent named 'main' — reserved")
            return

        agent_home = self._main_paths.ductor_home / "agents" / name
        config = merge_sub_agent_config(self._main_config, sub_cfg, agent_home)

        try:
            stack = await AgentStack.create(name, config)
        except Exception:
            logger.exception("Failed to create sub-agent '%s'", name)
            return

        self._stacks[name] = stack
        self._health[name] = AgentHealth(name=name)
        if self._bus:
            self._bus.register(name, stack)

        self._tasks[name] = asyncio.create_task(
            self._supervised_run(name, stack),
            name=f"agent:{name}",
        )
        logger.info("Sub-agent '%s' started (home=%s)", name, agent_home)

    async def stop_agent(self, name: str) -> None:
        """Stop a sub-agent gracefully."""
        if name == "main":
            logger.warning("Cannot stop main agent via stop_agent()")
            return

        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        stack = self._stacks.pop(name, None)
        if stack:
            with contextlib.suppress(Exception):
                await stack.shutdown()

        if self._bus:
            self._bus.unregister(name)

        health = self._health.get(name)
        if health:
            health.mark_stopped()

        logger.info("Sub-agent '%s' stopped", name)

    async def start_agent_by_name(self, name: str) -> str:
        """Start a sub-agent by name from the registry. Returns status message."""
        if name in self._stacks:
            return f"Agent '{name}' is already running."

        agents = self._registry.load()
        match = next((a for a in agents if a.name == name), None)
        if match is None:
            return f"Agent '{name}' not found in agents.json."

        await self._start_sub_agent(match)
        return f"Agent '{name}' started."

    # -- FileWatcher callback -----------------------------------------------

    async def _on_agents_changed(self) -> None:
        """Called when agents.json mtime changes. Sync running agents."""
        desired = {a.name: a for a in self._registry.load()}
        current_sub = set(self._stacks.keys()) - {"main"}
        desired_names = set(desired.keys())

        # Start new agents
        for name in desired_names - current_sub:
            logger.info("agents.json: new agent '%s' detected, starting", name)
            await self._start_sub_agent(desired[name])

        # Stop removed agents
        for name in current_sub - desired_names:
            logger.info("agents.json: agent '%s' removed, stopping", name)
            await self.stop_agent(name)

        # Check for config changes on existing agents
        for name in desired_names & current_sub:
            sub_cfg = desired[name]
            existing = self._stacks.get(name)
            if existing is None:
                continue

            # Rebuild config and compare token (cheapest change detection)
            agent_home = self._main_paths.ductor_home / "agents" / name
            new_config = merge_sub_agent_config(self._main_config, sub_cfg, agent_home)
            if new_config.telegram_token != existing.config.telegram_token:
                logger.info("agents.json: agent '%s' token changed, restarting", name)
                await self.stop_agent(name)
                await self._start_sub_agent(sub_cfg)

    # -- Notifications ------------------------------------------------------

    async def _notify_main_agent(self, message: str) -> None:
        """Send a system notification to the main agent's Telegram users."""
        main = self._stacks.get("main")
        if main is None:
            return
        try:
            from ductor_bot.bot.sender import send_rich

            for uid in main.config.allowed_user_ids:
                await send_rich(main.bot._bot, uid, f"**[Supervisor]** {message}")
        except Exception:
            logger.exception("Failed to notify main agent")

    # -- Shutdown -----------------------------------------------------------

    async def stop_all(self) -> None:
        """Shut down all agents and cleanup."""
        self._running = False
        await self._watcher.stop()

        # Stop sub-agents first, then main
        sub_names = [n for n in list(self._stacks.keys()) if n != "main"]
        for name in sub_names:
            await self.stop_agent(name)

        # Stop main
        main_task = self._tasks.pop("main", None)
        if main_task and not main_task.done():
            main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await main_task

        main_stack = self._stacks.pop("main", None)
        if main_stack:
            with contextlib.suppress(Exception):
                await main_stack.shutdown()

        if self._bus:
            self._bus.unregister("main")

        # Stop internal API
        if self._internal_api:
            await self._internal_api.stop()

        logger.info("AgentSupervisor stopped (all agents shut down)")
