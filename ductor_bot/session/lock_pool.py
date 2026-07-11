"""Named-session lock pool with reference-counted eviction."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ductor_bot.session.named import NamedSessionRegistry

NamedLockKey = tuple[int, str]


class _HandoffMutex:
    """Async mutex with explicit waiter handoff and safe non-blocking acquire."""

    def __init__(self) -> None:
        self._owned = False
        self._waiters: deque[asyncio.Future[None]] = deque()

    async def acquire(self) -> None:
        if not self._owned and not self._waiters:
            self._owned = True
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self.release()
            else:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
            raise

    def try_acquire_nowait(self) -> bool:
        if self._owned or self._waiters:
            return False
        self._owned = True
        return True

    def release(self) -> None:
        if not self._owned:
            msg = "Lock is not acquired"
            raise RuntimeError(msg)
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.cancelled():
                continue
            waiter.set_result(None)
            return
        self._owned = False

    def locked(self) -> bool:
        return self._owned


@dataclass(slots=True)
class _Entry:
    lock: _HandoffMutex
    borrowers: int = 0
    waiters: int = 0
    pending_eviction: bool = False


class _TryHeldLock:
    def __init__(self, pool: NamedSessionLockPool, key: NamedLockKey, entry: _Entry) -> None:
        self._pool = pool
        self._key = key
        self._entry = entry

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._entry.lock.release()
        self._pool._release(self._key, self._entry)


class NamedSessionLockPool:
    """Per-(chat, named session) locks shared by named-session leaf paths."""

    def __init__(self, registry: NamedSessionRegistry | None = None) -> None:
        self._entries: dict[NamedLockKey, _Entry] = {}
        self._registry = registry

    def bind_registry(self, registry: NamedSessionRegistry) -> None:
        self._registry = registry

    @contextlib.asynccontextmanager
    async def acquire(self, key: NamedLockKey) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(_HandoffMutex())
            self._entries[key] = entry
        entry.borrowers += 1
        entry.waiters += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            entry.waiters -= 1
            yield
        finally:
            if not acquired and entry.waiters > 0:
                entry.waiters -= 1
            if acquired:
                entry.lock.release()
            self._release(key, entry)

    def try_acquire_nowait(self, key: NamedLockKey) -> _TryHeldLock | None:
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(_HandoffMutex())
            self._entries[key] = entry
        if entry.waiters or not entry.lock.try_acquire_nowait():
            return None
        entry.borrowers += 1
        return _TryHeldLock(self, key, entry)

    def is_locked(self, key: NamedLockKey) -> bool:
        entry = self._entries.get(key)
        return bool(entry and entry.lock.locked())

    def has_waiters(self, key: NamedLockKey) -> bool:
        entry = self._entries.get(key)
        return bool(entry and entry.waiters > 0)

    def evict_if_unused(self, key: NamedLockKey) -> bool:
        entry = self._entries.get(key)
        if entry is None:
            return True
        if entry.borrowers == 0 and entry.waiters == 0 and self._is_ended(key):
            self._entries.pop(key, None)
            return True
        entry.pending_eviction = True
        return False

    def __len__(self) -> int:
        return len(self._entries)

    def _release(self, key: NamedLockKey, entry: _Entry) -> None:
        entry.borrowers = max(0, entry.borrowers - 1)
        if entry.borrowers == 0 and entry.waiters == 0 and entry.pending_eviction:
            if self._is_ended(key):
                self._entries.pop(key, None)
            else:
                entry.pending_eviction = False

    def _is_ended(self, key: NamedLockKey) -> bool:
        if self._registry is None:
            return False
        ns = self._registry.get(*key)
        return ns is None or ns.status == "ended"
