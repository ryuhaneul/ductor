"""Background task observer: fire-and-forget CLI execution with notification."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.background.models import BackgroundResult, BackgroundSubmit, BackgroundTask
from ductor_bot.i18n import t
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task
from ductor_bot.session.named import is_interagent_session, named_process_label

if TYPE_CHECKING:
    from ductor_bot.cli.param_resolver import TaskExecutionConfig
    from ductor_bot.cli.process_registry import ProcessRegistry
    from ductor_bot.cli.service import CLIService
    from ductor_bot.interagent_types import IARunningLimiter
    from ductor_bot.session.lock_pool import NamedSessionLockPool
    from ductor_bot.session.named import NamedSessionRegistry
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

BgResultCallback = Callable[[BackgroundResult], Awaitable[None]]

MAX_TASKS_PER_CHAT = 5


class BackgroundObserver:
    """Manages fire-and-forget background CLI tasks."""

    def __init__(  # noqa: PLR0913
        self,
        paths: DuctorPaths,
        *,
        timeout_seconds: float,
        cli_service: CLIService | None = None,
        named_sessions: NamedSessionRegistry | None = None,
        named_locks: NamedSessionLockPool | None = None,
        ia_limiter: IARunningLimiter | None = None,
        process_registry: ProcessRegistry | None = None,
    ) -> None:
        self._paths = paths
        self._timeout_seconds = timeout_seconds
        self._cli_service = cli_service
        self._named_sessions = named_sessions
        self._named_locks = named_locks
        self._ia_limiter = ia_limiter
        self._process_registry = process_registry
        self._on_result: BgResultCallback | None = None
        self._tasks: dict[str, BackgroundTask] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    def set_result_handler(self, handler: BgResultCallback) -> None:
        self._on_result = handler

    def submit(
        self,
        sub: BackgroundSubmit,
        exec_config: TaskExecutionConfig,
    ) -> str:
        """Submit a background task. Returns task_id."""
        active = sum(
            1
            for t in self._tasks.values()
            if t.chat_id == sub.chat_id and t.asyncio_task and not t.asyncio_task.done()
        )
        if active >= MAX_TASKS_PER_CHAT:
            msg = t("tasks.too_many", max=MAX_TASKS_PER_CHAT)
            raise ValueError(msg)

        task_id = secrets.token_hex(4)
        has_session_override = bool(sub.provider_override)
        bg_task = BackgroundTask(
            task_id=task_id,
            chat_id=sub.chat_id,
            prompt=sub.prompt,
            message_id=sub.message_id,
            thread_id=sub.thread_id,
            provider=sub.provider_override if has_session_override else exec_config.provider,
            model=sub.model_override if has_session_override else exec_config.model,
            submitted_at=time.monotonic(),
            session_name=sub.session_name,
            resume_session_id=sub.resume_session_id,
            reservation_gen=sub.reservation_gen,
            transport=sub.transport,
        )
        atask = asyncio.create_task(self._run(bg_task, exec_config))
        bg_task.asyncio_task = atask
        atask.add_done_callback(lambda done: self._on_task_done(task_id, bg_task, done))
        self._tasks[task_id] = bg_task
        logger.info(
            "Background task submitted id=%s chat=%d provider=%s session=%s",
            task_id,
            sub.chat_id,
            bg_task.provider,
            sub.session_name or "<stateless>",
        )
        return task_id

    def _on_task_done(
        self, task_id: str, bg_task: BackgroundTask, done: asyncio.Task[None]
    ) -> None:
        self._tasks.pop(task_id, None)
        if done.cancelled():
            cleanup = asyncio.create_task(self._finalize_cancelled(bg_task))
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)

    async def _drain_cleanups(self) -> None:
        await asyncio.sleep(0)
        while self._cleanup_tasks:
            pending = list(self._cleanup_tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)

    async def _rollback_reserved(self, bg_task: BackgroundTask) -> None:
        if self._named_sessions is None or self._named_locks is None:
            return
        async with self._named_locks.acquire((bg_task.chat_id, bg_task.session_name)):
            self._named_sessions.rollback_reservation(
                bg_task.chat_id, bg_task.session_name, bg_task.reservation_gen
            )
            self._named_sessions.prune_interagent(bg_task.chat_id, self._named_locks)

    async def _deliver_result(
        self,
        bg_task: BackgroundTask,
        status: str,
        text: str = "",
        session_id: str = "",
        *,
        elapsed_seconds: float | None = None,
    ) -> None:
        if bg_task.result_delivery_task is None:
            result = BackgroundResult(
                task_id=bg_task.task_id,
                chat_id=bg_task.chat_id,
                message_id=bg_task.message_id,
                thread_id=bg_task.thread_id,
                prompt_preview=bg_task.prompt[:60],
                result_text=text,
                status=status,
                elapsed_seconds=(
                    time.monotonic() - bg_task.submitted_at
                    if elapsed_seconds is None
                    else elapsed_seconds
                ),
                provider=bg_task.provider,
                model=bg_task.model,
                session_name=bg_task.session_name,
                session_id=session_id,
                transport=bg_task.transport,
            )
            bg_task.result_delivery_task = asyncio.create_task(self._deliver(result))
            bg_task.result_delivery_task.add_done_callback(
                lambda _done: setattr(bg_task, "result_delivery_complete", True)
            )
        await asyncio.shield(bg_task.result_delivery_task)

    async def _finalize_cancelled(self, bg_task: BackgroundTask) -> None:
        if bg_task.session_name:
            await self._rollback_reserved(bg_task)
        await self._deliver_result(bg_task, "aborted")

    def active_tasks(self, chat_id: int | None = None) -> list[BackgroundTask]:
        tasks = [t for t in self._tasks.values() if t.asyncio_task and not t.asyncio_task.done()]
        if chat_id is not None:
            tasks = [t for t in tasks if t.chat_id == chat_id]
        return tasks

    async def cancel_all(self, chat_id: int) -> int:
        count = 0
        cancelled: list[asyncio.Task[None]] = []
        finalized: list[BackgroundTask] = []
        for task in list(self._tasks.values()):
            if task.chat_id == chat_id and task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()
                cancelled.append(task.asyncio_task)
                finalized.append(task)
                count += 1
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        for task in finalized:
            await self._finalize_cancelled(task)
        await self._drain_cleanups()
        return count

    async def shutdown(self) -> None:
        cancelled: list[asyncio.Task[None]] = []
        finalized: list[BackgroundTask] = []
        for task in list(self._tasks.values()):
            if task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()
                cancelled.append(task.asyncio_task)
                finalized.append(task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        for task in finalized:
            await self._finalize_cancelled(task)
        await self._drain_cleanups()
        self._tasks.clear()

    async def _run(self, bg_task: BackgroundTask, exec_config: TaskExecutionConfig) -> None:
        if bg_task.session_name and self._cli_service:
            await self._run_with_session(bg_task)
        else:
            await self._run_oneshot(bg_task, exec_config)

    async def _run_oneshot(self, bg_task: BackgroundTask, exec_config: TaskExecutionConfig) -> None:
        """Legacy stateless execution via run_oneshot_task."""
        t0 = time.monotonic()
        try:
            result = await run_oneshot_task(
                exec_config,
                bg_task.prompt,
                TaskRunOptions(
                    cwd=self._paths.workspace,
                    timeout_seconds=self._timeout_seconds,
                    timeout_label="Background task",
                    ductor_home=self._paths.ductor_home,
                ),
            )

            elapsed = time.monotonic() - t0
            await self._deliver_result(
                bg_task,
                "error:cli_not_found" if result.execution is None else result.status,
                result.result_text,
                elapsed_seconds=elapsed,
            )
        except asyncio.CancelledError:
            elapsed = time.monotonic() - t0
            with contextlib.suppress(Exception):
                await self._deliver_result(bg_task, "aborted", elapsed_seconds=elapsed)
            raise
        except Exception:
            logger.exception("Background task failed id=%s", bg_task.task_id)
            elapsed = time.monotonic() - t0
            with contextlib.suppress(Exception):
                await self._deliver_result(
                    bg_task,
                    "error:internal",
                    t("tasks.internal_error"),
                    elapsed_seconds=elapsed,
                )

    async def _run_with_session(self, bg_task: BackgroundTask) -> None:  # noqa: C901, PLR0912, PLR0915
        """Named session execution via CLIService with resume support."""
        from ductor_bot.cli.types import AgentRequest

        assert self._cli_service is not None

        t0 = time.monotonic()
        permit = False
        lock_key = (bg_task.chat_id, bg_task.session_name)
        execution_token: str | None = None

        async def deliver(status: str, text: str = "", session_id: str = "") -> None:
            await self._deliver_result(
                bg_task,
                status,
                text,
                session_id,
                elapsed_seconds=time.monotonic() - t0,
            )

        try:
            if self._named_sessions is None or self._named_locks is None:
                execution_token = secrets.token_hex(8)
                request = AgentRequest(
                    prompt=bg_task.prompt,
                    model_override=bg_task.model or None,
                    provider_override=bg_task.provider or None,
                    chat_id=bg_task.chat_id,
                    topic_id=bg_task.thread_id,
                    transport=bg_task.transport,
                    process_label=named_process_label(bg_task.session_name, execution_token),
                    resume_session=bg_task.resume_session_id or None,
                    timeout_seconds=self._timeout_seconds,
                )
                response = await self._cli_service.execute(request)
            else:
                async with self._named_locks.acquire(lock_key):
                    ns = self._named_sessions.get(bg_task.chat_id, bg_task.session_name)
                    if ns is None or ns.status == "ended":
                        await deliver("aborted", "Named session ended before task execution.")
                        return
                    if ns.status != "running":
                        await deliver(
                            "error:superseded",
                            "Named session task was superseded; retry the request.",
                        )
                        return
                    if bg_task.reservation_gen and ns.reservation_gen != bg_task.reservation_gen:
                        logger.warning(
                            "Background reservation stale id=%s name=%s",
                            bg_task.task_id,
                            bg_task.session_name,
                        )
                        await deliver(
                            "error:superseded",
                            "Named session task was superseded; retry the request.",
                        )
                        return
                    if is_interagent_session(ns) and self._ia_limiter is not None:
                        if not self._ia_limiter.try_acquire():
                            self._named_sessions.rollback_reservation(
                                bg_task.chat_id, bg_task.session_name, bg_task.reservation_gen
                            )
                            logger.warning("IA running ceiling reached for background session")
                            await deliver(
                                "error:ceiling",
                                "Inter-agent running limit reached; retry the request later.",
                            )
                            return
                        permit = True
                    execution_token = self._named_sessions.begin_execution(
                        bg_task.chat_id, bg_task.session_name
                    )
                    request = AgentRequest(
                        prompt=bg_task.prompt,
                        model_override=bg_task.model or None,
                        provider_override=bg_task.provider or None,
                        chat_id=bg_task.chat_id,
                        topic_id=bg_task.thread_id,
                        transport=bg_task.transport or ns.transport,
                        process_label=named_process_label(
                            bg_task.session_name, execution_token
                        ),
                        resume_session=ns.session_id or None,
                        timeout_seconds=self._timeout_seconds,
                    )
                    response = await self._cli_service.execute(request)
                    if ns.status == "ended":
                        await deliver("aborted")
                        return
                    status = "ok"
                    if response.is_error:
                        status = "error:timeout" if response.timed_out else "error:cli"
                    self._named_sessions.update_after_response(
                        bg_task.chat_id,
                        bg_task.session_name,
                        response.session_id or "",
                        expected_session=ns,
                        reservation_gen=bg_task.reservation_gen or None,
                    )
                    self._named_sessions.prune_interagent(bg_task.chat_id, self._named_locks)
                    await deliver(status, response.result or "", response.session_id or "")
                    return

            status = "ok"
            if response.is_error:
                status = "error:timeout" if response.timed_out else "error:cli"
            await deliver(status, response.result or "", response.session_id or "")
        except asyncio.CancelledError:
            if self._named_sessions is not None and self._named_locks is not None:
                await self._rollback_reserved(bg_task)
            with contextlib.suppress(Exception):
                await deliver("aborted")
            raise
        except Exception:
            logger.exception(
                "Named session task failed id=%s name=%s", bg_task.task_id, bg_task.session_name
            )
            if self._named_sessions is not None and self._named_locks is not None:
                await self._rollback_reserved(bg_task)
            with contextlib.suppress(Exception):
                await deliver("error:internal", t("tasks.internal_error"))
        finally:
            if execution_token is not None:
                label = named_process_label(bg_task.session_name, execution_token)
                if self._process_registry is not None:
                    self._process_registry.clear_label_abort(bg_task.chat_id, label)
                if self._named_sessions is not None:
                    self._named_sessions.finish_execution(
                        bg_task.chat_id, bg_task.session_name, execution_token
                    )
            if permit and self._ia_limiter is not None:
                self._ia_limiter.release()

    async def _deliver(self, result: BackgroundResult) -> None:
        if self._on_result is None:
            logger.warning("No result handler set for background task %s", result.task_id)
            return
        try:
            await self._on_result(result)
        except Exception:
            logger.exception("Error delivering background result id=%s", result.task_id)
