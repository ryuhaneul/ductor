"""Tests for process registry."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.process_registry import ProcessRegistry, TrackedProcess, _kill_process_tree


def _mock_process(*, pid: int = 1, returncode: int | None = None) -> MagicMock:
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.pid = pid
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.send_signal = MagicMock()
    return proc


def test_register_returns_tracked() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=42)
    tracked = reg.register(chat_id=1, process=proc, label="main")
    assert isinstance(tracked, TrackedProcess)
    assert tracked.chat_id == 1
    assert tracked.label == "main"


def test_unregister_removes_process() -> None:
    reg = ProcessRegistry()
    proc = _mock_process()
    tracked = reg.register(chat_id=1, process=proc, label="main")
    reg.unregister(tracked)


def test_unregister_idempotent() -> None:
    reg = ProcessRegistry()
    proc = _mock_process()
    tracked = reg.register(chat_id=1, process=proc, label="main")
    reg.unregister(tracked)
    reg.unregister(tracked)  # no error


async def test_kill_all() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=10)
    reg.register(chat_id=1, process=proc, label="main")
    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        count = await reg.kill_all(chat_id=1)
    assert count == 1


async def test_kill_all_sets_aborted() -> None:
    reg = ProcessRegistry()
    proc = _mock_process()
    reg.register(chat_id=1, process=proc, label="main")
    assert reg.was_aborted(1) is False
    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        await reg.kill_all(chat_id=1)
    assert reg.was_aborted(1) is True


def test_clear_abort() -> None:
    reg = ProcessRegistry()
    reg._aborted.add(1)
    assert reg.was_aborted(1) is True
    reg.clear_abort(1)
    assert reg.was_aborted(1) is False


async def test_kill_all_empty_returns_zero() -> None:
    reg = ProcessRegistry()
    count = await reg.kill_all(chat_id=999)
    assert count == 0


async def test_kill_all_active_across_chats() -> None:
    reg = ProcessRegistry()
    proc1 = _mock_process(pid=11)
    proc2 = _mock_process(pid=12)
    reg.register(chat_id=1, process=proc1, label="main")
    reg.register(chat_id=2, process=proc2, label="main")

    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        count = await reg.kill_all_active()

    assert count == 2
    assert reg.has_active(1) is False
    assert reg.has_active(2) is False
    assert reg.was_aborted(1) is True
    assert reg.was_aborted(2) is True


def test_multiple_chats_isolated() -> None:
    reg = ProcessRegistry()
    proc1 = _mock_process(pid=1)
    proc2 = _mock_process(pid=2)
    reg.register(chat_id=1, process=proc1, label="main")
    reg.register(chat_id=2, process=proc2, label="main")
    assert reg.has_active(1) is True
    assert reg.has_active(2) is True
    assert reg.has_active(3) is False


def test_unregister_ignores_foreign_tracked_same_chat() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=11)
    reg.register(chat_id=1, process=proc, label="main")
    foreign = TrackedProcess(process=proc, chat_id=1, label="main")
    reg.unregister(foreign)  # no error
    assert reg.has_active(1) is True


async def test_kill_stale_returns_zero_when_none_stale() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=21)
    reg.register(chat_id=1, process=proc, label="main")
    killed = await reg.kill_stale(max_age_seconds=9999)
    assert killed == 0


async def test_kill_stale_kills_and_unregisters_old_entries() -> None:
    reg = ProcessRegistry()
    old_proc = _mock_process(pid=30)
    fresh_proc = _mock_process(pid=31)
    done_proc = _mock_process(pid=32, returncode=0)

    old = reg.register(chat_id=1, process=old_proc, label="old")
    fresh = reg.register(chat_id=1, process=fresh_proc, label="fresh")
    reg.register(chat_id=1, process=done_proc, label="done")
    old.registered_at = time.time() - 1000
    fresh.registered_at = time.time()

    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        killed = await reg.kill_stale(max_age_seconds=60)

    assert killed == 1
    assert old_proc.terminate.called
    assert reg.has_active(1) is True  # fresh process remains


async def test_kill_stale_handles_process_lookup_error() -> None:
    reg = ProcessRegistry()
    proc = _mock_process(pid=40)
    proc.terminate.side_effect = ProcessLookupError()
    tracked = reg.register(chat_id=1, process=proc, label="gone")
    tracked.registered_at = time.time() - 1000

    with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
        killed = await reg.kill_stale(max_age_seconds=60)

    assert killed == 0


def test_kill_process_tree_swallows_oserror() -> None:
    with patch("ductor_bot.cli.process_registry.subprocess.run", side_effect=OSError()):
        _kill_process_tree(1234)
