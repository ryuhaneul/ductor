"""Centralized registry for active CLI subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from ductor_bot.infra.process_tree import (
    force_kill_process_tree,
    interrupt_process,
    terminate_process_tree,
)

logger = logging.getLogger(__name__)

_SIGTERM_GRACE_SECONDS = 2.0


@dataclass(slots=True)
class TrackedProcess:
    """A registered subprocess with metadata."""

    process: asyncio.subprocess.Process
    chat_id: int
    label: str
    topic_id: int | None = None
    registered_at: float = field(default_factory=time.time)


class ProcessRegistry:
    """Global registry of active CLI subprocesses, keyed by *chat_id*."""

    def __init__(self) -> None:
        self._processes: dict[int, list[TrackedProcess]] = {}
        self._aborted: set[int] = set()
        self._aborted_topics: set[tuple[int, int | None]] = set()
        self._aborted_labels: set[tuple[int, str]] = set()
        self._interrupted: set[int] = set()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        # MED #9: serialize bulk kill operations (kill_for_task / kill_stale /
        # kill_by_label) against each other so a concurrent ``register`` that
        # slips in mid-iteration can't orphan subprocesses past a cancel.
        # ``register`` itself stays lock-free — a single dict.setdefault()+
        # list.append() pair is atomic under the GIL.
        self._kill_lock: asyncio.Lock = asyncio.Lock()

    def register(
        self,
        chat_id: int,
        process: asyncio.subprocess.Process,
        label: str,
        *,
        topic_id: int | None = None,
    ) -> TrackedProcess:
        """Register a subprocess. Returns the tracking handle.

        Lock-free on purpose: ``dict.setdefault`` + ``list.append`` is atomic
        under the GIL. Bulk kill operations (:meth:`kill_for_task`,
        :meth:`kill_stale`, :meth:`kill_by_label`) serialize against each
        other via :attr:`_kill_lock`, which is sufficient — a register that
        happens between two kills simply belongs to the next cancel round.
        """
        tracked = TrackedProcess(
            process=process,
            chat_id=chat_id,
            label=label,
            topic_id=topic_id,
        )
        if (chat_id, label) in self._aborted_labels:
            self._processes.setdefault(chat_id, []).append(tracked)
            cleanup = asyncio.create_task(self._kill_late_registered(tracked))
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            logger.info(
                "Late process queued for forced cleanup: chat=%d label=%s pid=%s",
                chat_id,
                label,
                process.pid,
            )
            return tracked
        self._processes.setdefault(chat_id, []).append(tracked)
        logger.debug(
            "Process registered: chat=%d label=%s pid=%s",
            chat_id,
            label,
            process.pid,
        )
        return tracked

    async def _kill_late_registered(self, tracked: TrackedProcess) -> None:
        try:
            await _kill_processes([tracked])
        finally:
            self.unregister(tracked)

    async def drain_cleanup_tasks(self) -> None:
        """Wait for pending late-process kill ladders to finish."""
        while self._cleanup_tasks:
            await asyncio.gather(*list(self._cleanup_tasks), return_exceptions=True)

    def unregister(self, tracked: TrackedProcess) -> None:
        """Remove a tracked process (idempotent)."""
        entries = self._processes.get(tracked.chat_id)
        if entries is None:
            return
        try:
            entries.remove(tracked)
        except ValueError:
            return
        if not entries:
            del self._processes[tracked.chat_id]
        logger.debug(
            "Process unregistered: chat=%d label=%s pid=%s",
            tracked.chat_id,
            tracked.label,
            tracked.process.pid,
        )

    async def kill_all(self, chat_id: int) -> int:
        """Kill every active process for *chat_id*. Returns count killed."""
        self._aborted.add(chat_id)
        entries = self._processes.pop(chat_id, [])
        if not entries:
            return 0
        return await _kill_processes(entries)

    async def kill_by_chat_topic(self, chat_id: int, topic_id: int | None) -> int:
        """Kill processes belonging to *chat_id* + *topic_id*. Returns count.

        Used by ``/stop`` so a stop in one topic does not affect another
        topic in the same chat. ``topic_id=None`` matches processes
        registered without a topic (e.g. private chats).
        """
        async with self._kill_lock:
            entries = self._processes.get(chat_id, [])
            targets = [
                t
                for t in entries
                if not t.label.startswith("ns:")
                and t.topic_id == topic_id
                and t.process.returncode is None
            ]
            if not targets:
                return 0
            self._aborted_topics.add((chat_id, topic_id))
            remaining = [
                t
                for t in entries
                if t.label.startswith("ns:")
                or t.topic_id != topic_id
                or t.process.returncode is not None
            ]
            if remaining:
                self._processes[chat_id] = remaining
            else:
                self._processes.pop(chat_id, None)
        return await _kill_processes(targets)

    async def kill_all_active(self) -> int:
        """Kill active processes across all chats. Returns total count killed."""
        total = 0
        for chat_id in list(self._processes):
            total += await self.kill_all(chat_id)
        return total

    def was_aborted(self, chat_id: int) -> bool:
        """Check whether *chat_id* has been aborted since last clear."""
        return chat_id in self._aborted

    def clear_abort(self, chat_id: int) -> None:
        """Clear the abort flag for *chat_id*."""
        self._aborted.discard(chat_id)

    def was_aborted_topic(self, chat_id: int, topic_id: int | None) -> bool:
        """Check whether *(chat_id, topic_id)* has been aborted since last clear."""
        return (chat_id, topic_id) in self._aborted_topics

    def clear_topic_abort(self, chat_id: int, topic_id: int | None) -> None:
        """Clear the topic-scoped abort flag (called after handling the abort)."""
        self._aborted_topics.discard((chat_id, topic_id))

    def was_interrupted(self, chat_id: int) -> bool:
        """Check whether *chat_id* was soft-interrupted since last clear."""
        return chat_id in self._interrupted

    def clear_interrupt(self, chat_id: int) -> None:
        """Clear the interrupt flag for *chat_id*."""
        self._interrupted.discard(chat_id)

    def has_active(self, chat_id: int, topic_id: int | None = None) -> bool:
        """Return True if *chat_id* has at least one running subprocess.

        When *topic_id* is given, only processes for that specific topic are
        checked.  When ``None``, all processes for *chat_id* are considered.
        """
        entries = self._processes.get(chat_id, [])
        if topic_id is not None:
            return any(e.process.returncode is None and e.topic_id == topic_id for e in entries)
        return any(e.process.returncode is None for e in entries)

    async def kill_by_label(self, chat_id: int, label: str) -> int:
        """Kill processes matching *label* for *chat_id*. Returns count killed."""
        async with self._kill_lock:
            self._aborted_labels.add((chat_id, label))
            entries = self._processes.get(chat_id, [])
            to_kill = [e for e in entries if e.label == label and e.process.returncode is None]
            if not to_kill:
                return 0
            remaining = [e for e in entries if e not in to_kill]
            if remaining:
                self._processes[chat_id] = remaining
            else:
                self._processes.pop(chat_id, None)
            return await _kill_processes(to_kill)

    def clear_label_abort(self, chat_id: int, label: str) -> None:
        """Clear the abort flag for a specific label."""
        self._aborted_labels.discard((chat_id, label))

    def was_aborted_label(self, chat_id: int, label: str) -> bool:
        """Check whether a generation-scoped process label was aborted."""
        return (chat_id, label) in self._aborted_labels

    def interrupt_all(self, chat_id: int) -> int:
        """Send SIGINT to every active process for *chat_id*.

        Unlike :meth:`kill_all` this does NOT terminate or unregister the
        processes — it sends a soft interrupt so the CLI can cancel the
        current operation (equivalent to pressing ESC in the terminal).
        Returns the count of processes signalled.
        """
        entries = self._processes.get(chat_id, [])
        if not entries:
            return 0
        self._interrupted.add(chat_id)
        count = 0
        for tracked in entries:
            if tracked.process.returncode is not None:
                continue
            interrupt_process(tracked.process.pid)
            logger.debug(
                "SIGINT sent: pid=%s label=%s chat=%d",
                tracked.process.pid,
                tracked.label,
                tracked.chat_id,
            )
            count += 1
        if count:
            logger.info("Interrupted %d CLI process(es) for chat=%d", count, chat_id)
        return count

    async def kill_stale(self, max_age_seconds: float) -> int:
        """Kill processes older than *max_age_seconds* (wall-clock). Returns count killed."""
        async with self._kill_lock:
            now = time.time()
            stale: list[TrackedProcess] = []
            for entries in self._processes.values():
                for tracked in entries:
                    if tracked.process.returncode is not None:
                        continue
                    age = now - tracked.registered_at
                    if age > max_age_seconds:
                        logger.warning(
                            "Stale process: pid=%s label=%s chat=%d age=%.0fs",
                            tracked.process.pid,
                            tracked.label,
                            tracked.chat_id,
                            age,
                        )
                        stale.append(tracked)
            if not stale:
                return 0
            killed = await _kill_processes(stale)
            for tracked in stale:
                self.unregister(tracked)
            return killed

    async def kill_for_task(self, task_id: str) -> int:
        """Kill any tracked process labelled ``f'task:{task_id}'``. Returns count killed.

        Mirrors :meth:`kill_stale` semantics but filters by label. Uses the
        SIGTERM → 2s → SIGKILL ladder via :func:`_kill_processes`. Already-exited
        processes are skipped (``returncode is not None``). Each killed entry is
        unregistered after the ladder completes.

        MED #9: the collect → kill → unregister sequence runs under
        :attr:`_kill_lock` so a subprocess that registers mid-iteration
        cannot slip past the cancel. ``register`` remains lock-free.
        """
        label = f"task:{task_id}"
        async with self._kill_lock:
            targets: list[TrackedProcess] = []
            for entries in self._processes.values():
                for tracked in entries:
                    if tracked.process.returncode is not None:
                        continue
                    if tracked.label == label:
                        targets.append(tracked)
            if not targets:
                return 0
            killed = await _kill_processes(targets)
            for tracked in targets:
                self.unregister(tracked)
            return killed


def _send_sigterm(entries: list[TrackedProcess]) -> int:
    """Terminate all live processes. Returns count signalled."""
    count = 0
    for tracked in entries:
        if tracked.process.returncode is not None:
            continue
        try:
            _close_stdin(tracked.process)
            terminate_process_tree(tracked.process.pid)
            logger.debug("Terminate sent: pid=%s label=%s", tracked.process.pid, tracked.label)
            count += 1
        except ProcessLookupError:
            pass
    return count


def _send_sigkill(entries: list[TrackedProcess]) -> None:
    """Send SIGKILL to processes still alive after grace period."""
    for tracked in entries:
        if tracked.process.returncode is not None:
            continue
        try:
            _close_stdin(tracked.process)
            force_kill_process_tree(tracked.process.pid)
            logger.debug("SIGKILL sent: pid=%s label=%s", tracked.process.pid, tracked.label)
        except ProcessLookupError:
            pass


async def _reap(entries: list[TrackedProcess]) -> None:
    """Wait for all processes to exit."""
    for tracked in entries:
        if tracked.process.returncode is None:
            try:
                await asyncio.wait_for(tracked.process.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning("Process did not exit after SIGKILL: pid=%s", tracked.process.pid)


async def _kill_processes(entries: list[TrackedProcess]) -> int:
    """SIGTERM -> wait -> SIGKILL for each process. Returns count killed."""
    if not entries:
        return 0
    killed = _send_sigterm(entries)
    if not killed:
        return 0
    await asyncio.sleep(_SIGTERM_GRACE_SECONDS)
    _send_sigkill(entries)
    await _reap(entries)
    logger.info("Killed %d CLI process(es)", killed)
    return killed


def _close_stdin(process: asyncio.subprocess.Process) -> None:
    """Best-effort stdin close so readers can unwind promptly."""
    stdin = getattr(process, "stdin", None)
    if stdin is None:
        return
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        stdin.close()
