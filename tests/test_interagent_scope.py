"""Regression tests for topic-scoped inter-agent named sessions."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.background.models import BackgroundResult, BackgroundSubmit, BackgroundTask
from ductor_bot.background.observer import BackgroundObserver
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.interagent_types import IARunningLimiter, InterAgentOrigin
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.orchestrator.core import NamedSessionRequest, Orchestrator
from ductor_bot.orchestrator.injection import _derive_ia_name
from ductor_bot.session.lock_pool import NamedSessionLockPool
from ductor_bot.session.named import NamedSession, NamedSessionRegistry, is_interagent_session
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths


def _setup_framework(fw_root: Path) -> None:
    ws = fw_root / "workspace"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("# Ductor Home")
    config_dir = ws / "config"
    config_dir.mkdir()
    inner = ws / "workspace"
    inner.mkdir()
    (inner / "CLAUDE.md").write_text("# Framework CLAUDE.md")
    for subdir in ("memory_system", "cron_tasks", "output_to_user", "telegram_files"):
        d = inner / subdir
        d.mkdir()
        (d / "CLAUDE.md").write_text(f"# {subdir}")
    (inner / "memory_system" / "MAINMEMORY.md").write_text("# Main Memory\n")
    tools = inner / "tools"
    tools.mkdir()
    (tools / "CLAUDE.md").write_text("# Tools")
    (fw_root / "config.example.json").write_text('{"provider": "claude", "model": "opus"}')


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[DuctorPaths, AgentConfig]:
    fw_root = tmp_path / "fw"
    _setup_framework(fw_root)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw_root / "workspace", framework_root=fw_root
    )
    init_workspace(paths)
    return paths, AgentConfig()


def _orch(workspace: tuple[DuctorPaths, AgentConfig]) -> Orchestrator:
    paths, config = workspace
    config.allowed_user_ids = [12345]
    config.transport = "telegram"
    orch = Orchestrator(config, paths, agent_name="codex")
    cli = MagicMock()
    cli._config = MagicMock(agent_name="codex", cli_timeout=120)
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="sess-1", result="done"))
    object.__setattr__(orch, "_cli_service", cli)
    return orch


def test_scoped_name_derivation_slug_budget_and_legacy() -> None:
    legacy = _derive_ia_name("Main.Agent", None)
    assert legacy == "ia-Main.Agent"

    origin = InterAgentOrigin("tg", 123, 456)
    name = _derive_ia_name("Main.Agent:With Symbols", origin)
    assert name.startswith("ia.mainagentwithsy")
    assert ".t456." in name
    assert len(name.encode()) <= 40

    long_topic = _derive_ia_name("sender", InterAgentOrigin("tg", 123, 10**30))
    assert long_topic.startswith("ia.sender")
    assert len(long_topic.encode()) <= 40


async def test_field_lookup_scopes_same_sender_by_topic(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    o1 = InterAgentOrigin("tg", 777, 1)
    o2 = InterAgentOrigin("tg", 777, 2)

    first = await orch.handle_interagent_message("main", "one", origin=o1)
    second = await orch.handle_interagent_message("main", "two", origin=o2)
    again = await orch.handle_interagent_message("main", "again", origin=o1)

    assert first.session_name != second.session_name
    assert again.session_name == first.session_name
    assert orch._named_sessions.get(12345, first.session_name).ia_topic_id == 1
    assert orch._named_sessions.get(12345, second.session_name).ia_topic_id == 2


async def test_new_session_running_same_identity_returns_busy_without_wait(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(_request: object) -> CLIResponse:
        entered.set()
        await release.wait()
        return CLIResponse(session_id="sess-slow", result="slow done")

    orch._cli_service.execute = AsyncMock(side_effect=slow)
    origin = InterAgentOrigin("tg", 777, 5)
    running = asyncio.create_task(orch.handle_interagent_message("main", "one", origin=origin))
    await asyncio.wait_for(entered.wait(), timeout=1)

    busy = await asyncio.wait_for(
        orch.handle_interagent_message("main", "two", new_session=True, origin=origin),
        timeout=0.5,
    )
    assert busy.ok is False
    assert busy.error_kind == "busy"

    release.set()
    done = await running
    assert done.ok is True


async def test_ceiling_rejection_is_typed_and_cleans_new_session(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    orch._ia_limiter = IARunningLimiter(0)

    outcome = await orch.handle_interagent_message(
        "main",
        "one",
        origin=InterAgentOrigin("tg", 777, 9),
    )

    assert outcome.ok is False
    assert outcome.error_kind == "ceiling"
    assert orch._named_sessions.get(12345, outcome.session_name).status == "ended"


async def test_execution_exception_is_typed_failure(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    orch._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))

    outcome = await orch.handle_interagent_message("main", "boom")

    assert outcome.ok is False
    assert outcome.error_kind == "execution"
    assert "Error processing" in outcome.text


async def test_lock_pool_deferred_eviction(tmp_path: Path) -> None:
    reg = NamedSessionRegistry(tmp_path / "named.json")
    ns = NamedSession(
        name="ia-main",
        chat_id=1,
        provider="codex",
        model="gpt",
        session_id="sid",
        prompt_preview="p",
        status="idle",
        created_at=1.0,
    )
    reg.add(ns)
    pool = NamedSessionLockPool(reg)

    async with pool.acquire((1, "ia-main")):
        reg.end_session(1, "ia-main")
        assert pool.evict_if_unused((1, "ia-main")) is False
        assert len(pool) == 1
    assert len(pool) == 0


def test_prune_uses_last_active_and_legacy_predicate(tmp_path: Path) -> None:
    reg = NamedSessionRegistry(tmp_path / "named.json")
    pool = NamedSessionLockPool(reg)
    for i in range(34):
        reg.add(
            NamedSession(
                name=f"ia.sender{i}.xabcdef" if i % 2 else f"ia-legacy{i}",
                chat_id=1,
                provider="codex",
                model="gpt",
                session_id="sid",
                prompt_preview="p",
                status="idle",
                created_at=float(i),
                ia_sender=f"s{i}" if i % 2 else None,
                ia_transport="tg" if i % 2 else None,
                ia_chat_id=10 if i % 2 else None,
                last_active_at=float(i),
            )
        )

    ended = reg.prune_interagent(1, pool)

    assert len(ended) == 2
    assert "ia-legacy0" in ended
    assert all(is_interagent_session(s) for s in reg._sessions.values() if s.name.startswith("ia"))


async def test_background_prestart_cancel_shutdown_rolls_back_reservation(tmp_path: Path) -> None:
    reg = NamedSessionRegistry(tmp_path / "named.json")
    ns = NamedSession(
        name="work",
        chat_id=1,
        provider="codex",
        model="gpt",
        session_id="sid",
        prompt_preview="p",
        status="idle",
        created_at=1.0,
    )
    reg.add(ns)
    gen = reg.reserve_followup(1, "work", "prompt")
    assert gen is not None
    locks = NamedSessionLockPool(reg)
    cli = MagicMock()
    cli.execute = AsyncMock(side_effect=asyncio.CancelledError())
    observer = BackgroundObserver(
        tmp_path,
        timeout_seconds=5,
        cli_service=cli,
        named_sessions=reg,
        named_locks=locks,
        ia_limiter=IARunningLimiter(16),
    )
    results = AsyncMock()
    observer.set_result_handler(results)
    config = TaskExecutionConfig(
        provider="codex",
        model="gpt",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="default",
        working_dir=str(tmp_path),
        file_access="workspace",
    )
    observer.submit(
        BackgroundSubmit(
            chat_id=1,
            prompt="prompt",
            message_id=1,
            thread_id=None,
            session_name="work",
            resume_session_id="sid",
            provider_override="codex",
            model_override="gpt",
            reservation_gen=gen,
        ),
        config,
    )
    await observer.shutdown()

    assert reg.get(1, "work").status == "idle"
    results.assert_awaited_once()
    assert results.await_args.args[0].status == "aborted"


def test_async_hint_depends_on_manual_resume_supported() -> None:
    from ductor_bot.bus.adapters import build_interagent_injection_prompt

    result = AsyncInterAgentResult(
        task_id="t1",
        sender="main",
        recipient="dev",
        message_preview="m",
        result_text="done",
        session_name="ia-main",
        manual_resume_supported=False,
    )
    assert "@ia-main" not in build_interagent_injection_prompt(
        result, agent_name="main", transport_label="Matrix room"
    )
    result.manual_resume_supported = True
    assert "@ia-main" in build_interagent_injection_prompt(
        result, agent_name="main", transport_label="Telegram chat"
    )


def test_ask_agent_sync_forwards_origin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = (
        Path(__file__).resolve().parents[1]
        / "ductor_bot/_home_defaults/workspace/tools/agent_tools/ask_agent.py"
    )
    spec = importlib.util.spec_from_file_location("ask_agent_tool", tool)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"success": True, "text": "ok"}).encode()

    def fake_urlopen(req: object, timeout: int) -> _Resp:
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["ask_agent.py", "dev", "hello"])
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "main")
    monkeypatch.setenv("DUCTOR_CHAT_ID", "111")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "222")
    monkeypatch.setenv("DUCTOR_TRANSPORT", "tg")

    mod.main()

    assert captured["body"]["chat_id"] == 111
    assert captured["body"]["topic_id"] == 222
    assert captured["body"]["transport"] == "tg"


def _ia_session(
    name: str,
    *,
    chat_id: int = 1,
    status: str = "idle",
    sender: str | None = "main",
    topic_id: int | None = 1,
    created_at: float = 1.0,
    last_active_at: float | None = 1.0,
) -> NamedSession:
    return NamedSession(
        name=name,
        chat_id=chat_id,
        provider="codex",
        model="gpt-old",
        session_id="sid",
        prompt_preview="p",
        status=status,
        created_at=created_at,
        ia_sender=sender,
        ia_transport="tg" if sender is not None else None,
        ia_chat_id=777 if sender is not None else None,
        ia_topic_id=topic_id if sender is not None else None,
        last_active_at=last_active_at,
    )


def test_old_json_defaults_asdict_roundtrip_and_recovery_excludes_ia(tmp_path: Path) -> None:
    from dataclasses import asdict

    path = tmp_path / "named.json"
    path.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "name": "ia-main",
                        "chat_id": 1,
                        "provider": "codex",
                        "model": "gpt",
                        "session_id": "sid",
                        "prompt_preview": "p",
                        "status": "running",
                        "created_at": 1.0,
                    },
                    {
                        "name": "normal",
                        "chat_id": 1,
                        "provider": "codex",
                        "model": "gpt",
                        "session_id": "sid2",
                        "prompt_preview": "p",
                        "status": "running",
                        "created_at": 2.0,
                    },
                ]
            }
        )
    )
    reg = NamedSessionRegistry(path)
    legacy = reg.get(1, "ia-main")
    assert legacy is not None
    assert legacy.status == "idle"
    assert legacy.ia_sender is None
    assert legacy.reservation_gen == 0
    assert legacy.last_active_at is None
    assert asdict(legacy)["ia_topic_id"] is None
    assert [s.name for s in reg.pop_recovered_running(1)] == ["normal"]
    assert NamedSessionRegistry(path).get(1, "ia-main") is not None


async def test_identity_first_reuses_stored_name_when_derived_name_changes(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    stored = _ia_session("ia.main.t1.xcustom", chat_id=12345, topic_id=1)
    orch._named_sessions.add(stored)
    outcome = await asyncio.wait_for(
        orch.handle_interagent_message("main", "reuse", origin=InterAgentOrigin("tg", 777, 1)),
        timeout=1,
    )
    assert outcome.session_name == "ia.main.t1.xcustom"
    assert orch._cli_service.execute.await_count == 1


async def test_forced_hash_extension_avoids_different_identity_collision(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    import ductor_bot.orchestrator.injection as inj

    orch = _orch(workspace)

    class _Digest:
        def __init__(self, value: str) -> None:
            self.value = value

        def hexdigest(self) -> str:
            suffix = "11" if "sender!" in self.value else "22"
            return "abcdef" + suffix + "0" * 32

    monkeypatch.setattr(inj.hashlib, "sha1", lambda data, **_: _Digest(data.decode()))
    first = await orch.handle_interagent_message(
        "sender!", "one", origin=InterAgentOrigin("tg", 777, 1)
    )
    second = await orch.handle_interagent_message(
        "sender?", "two", origin=InterAgentOrigin("tg", 777, 1)
    )
    assert first.session_name.endswith(".xabcdef")
    assert second.session_name.endswith(".xabcdef22")
    assert first.session_name != second.session_name


async def test_new_session_running_reservation_returns_busy_without_ending_existing(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    ns = _ia_session("ia.main.t1.xstored", chat_id=12345, status="running", topic_id=1)
    orch._named_sessions.add(ns)
    outcome = await orch.handle_interagent_message(
        "main", "new", new_session=True, origin=InterAgentOrigin("tg", 777, 1)
    )
    assert outcome.ok is False
    assert outcome.error_kind == "busy"
    assert orch._named_sessions.get(12345, ns.name).status == "running"
    orch._cli_service.execute.assert_not_awaited()


async def test_same_key_serializes_and_different_keys_run_parallel(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    entered: list[str] = []
    release = asyncio.Event()

    async def slow(request: object) -> CLIResponse:
        entered.append(request.prompt.split("\n", 2)[1])
        if len(entered) == 1:
            await release.wait()
        return CLIResponse(session_id=f"sid-{len(entered)}", result="ok")

    orch._cli_service.execute = AsyncMock(side_effect=slow)
    first = asyncio.create_task(
        orch.handle_interagent_message("main", "same-1", origin=InterAgentOrigin("tg", 777, 1))
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        orch.handle_interagent_message("main", "same-2", origin=InterAgentOrigin("tg", 777, 1))
    )
    await asyncio.sleep(0.05)
    assert len(entered) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(entered) == 2

    entered.clear()
    release.clear()
    one = asyncio.create_task(
        orch.handle_interagent_message("main", "topic-1", origin=InterAgentOrigin("tg", 777, 10))
    )
    two = asyncio.create_task(
        orch.handle_interagent_message("main", "topic-2", origin=InterAgentOrigin("tg", 777, 20))
    )
    await asyncio.sleep(0.05)
    assert len(entered) == 2
    release.set()
    await asyncio.gather(one, two)


def test_model_persist_and_stale_reservation_update_suppression(tmp_path: Path) -> None:
    reg = NamedSessionRegistry(tmp_path / "named.json")
    ns = _ia_session("ia.main.x1", status="running")
    ns.reservation_gen = 3
    reg.add(ns)
    assert (
        reg.update_after_response(1, ns.name, "new", expected_session=ns, reservation_gen=2)
        is False
    )
    assert reg.get(1, ns.name).status == "running"
    assert reg.update_model(1, ns.name, "gpt-new") is True
    assert NamedSessionRegistry(tmp_path / "named.json").get(1, ns.name).model == "gpt-new"


async def test_submit_exception_and_resolver_failure_rollback_reservation(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = _orch(workspace)
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "idle", 1.0)
    orch._named_sessions.add(ns)
    orch._observers.background = MagicMock()
    orch._observers.background.submit.side_effect = RuntimeError("submit failed")
    with pytest.raises(RuntimeError):
        await orch.submit_named_followup_bg(1, "work", "prompt", 1, None)
    assert orch._named_sessions.get(1, "work").status == "idle"
    orch._observers.background.submit.side_effect = None
    monkeypatch.setattr(
        "ductor_bot.cli.param_resolver.resolve_cli_config",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("resolve failed")),
    )
    with pytest.raises(RuntimeError):
        await orch.submit_named_followup_bg(1, "work", "prompt", 1, None)
    assert orch._named_sessions.get(1, "work").status == "idle"


async def test_background_lock_wait_cancel_rolls_back_reservation(tmp_path: Path) -> None:
    from ductor_bot.background.models import BackgroundTask

    reg = NamedSessionRegistry(tmp_path / "named.json")
    ns = _ia_session("ia.main.xlock", status="idle")
    reg.add(ns)
    gen = reg.reserve_followup(1, ns.name, "prompt")
    locks = NamedSessionLockPool(reg)
    observer = BackgroundObserver(
        tmp_path,
        timeout_seconds=5,
        cli_service=MagicMock(),
        named_sessions=reg,
        named_locks=locks,
        ia_limiter=IARunningLimiter(16),
    )
    bg_task = BackgroundTask(
        "t",
        1,
        "prompt",
        1,
        None,
        "codex",
        "gpt",
        0.0,
        session_name=ns.name,
        reservation_gen=gen or 0,
    )
    async with locks.acquire((1, ns.name)):
        runner = asyncio.create_task(observer._run_with_session(bg_task))
        await asyncio.sleep(0)
        runner.cancel()
    await asyncio.gather(runner, return_exceptions=True)
    assert reg.get(1, ns.name).status == "idle"


async def test_reused_session_ceiling_preserves_idle_and_exception_releases_permit(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    ns = _ia_session("ia.main.t1.xstored", chat_id=12345, status="idle", topic_id=1)
    ns.model, ns.provider = orch.resolve_runtime_target(orch._config.model)
    orch._named_sessions.add(ns)
    orch._ia_limiter = IARunningLimiter(0)
    outcome = await orch.handle_interagent_message(
        "main", "one", origin=InterAgentOrigin("tg", 777, 1)
    )
    assert outcome.error_kind == "ceiling"
    assert orch._named_sessions.get(12345, ns.name).status == "idle"
    orch._ia_limiter = IARunningLimiter(1)
    orch._cli_service.execute = AsyncMock(side_effect=RuntimeError("boom"))
    failed = await orch.handle_interagent_message("main", "boom")
    assert failed.ok is False
    assert failed.error_kind == "execution"
    orch._cli_service.execute = AsyncMock(return_value=CLIResponse(session_id="sid", result="ok"))
    assert (await orch.handle_interagent_message("main", "after")).ok is True


async def test_foreground_streaming_and_background_limiter_enforcement(
    workspace: tuple[DuctorPaths, AgentConfig], tmp_path: Path
) -> None:
    from ductor_bot.background.models import BackgroundTask
    from ductor_bot.orchestrator.flows import named_session_flow, named_session_streaming
    from ductor_bot.session.key import SessionKey

    orch = _orch(workspace)
    ns = _ia_session("ia.main.xmanual", chat_id=1, status="idle")
    orch._named_sessions.add(ns)
    orch._ia_limiter = IARunningLimiter(0)
    assert (
        "ceiling"
        in (await named_session_flow(orch, SessionKey(chat_id=1), ns.name, "hi")).text.lower()
    )
    assert (
        "ceiling"
        in (await named_session_streaming(orch, SessionKey(chat_id=1), ns.name, "hi")).text.lower()
    )
    orch._cli_service.execute.assert_not_awaited()

    reg = NamedSessionRegistry(tmp_path / "named.json")
    bg_ns = _ia_session("ia.main.xbg", status="idle")
    reg.add(bg_ns)
    gen = reg.reserve_followup(1, bg_ns.name, "prompt")
    cli = MagicMock()
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="sid", result="ok"))
    observer = BackgroundObserver(
        tmp_path,
        timeout_seconds=5,
        cli_service=cli,
        named_sessions=reg,
        named_locks=NamedSessionLockPool(reg),
        ia_limiter=IARunningLimiter(0),
    )
    await observer._run_with_session(
        BackgroundTask(
            "t",
            1,
            "prompt",
            1,
            None,
            "codex",
            "gpt",
            0.0,
            session_name=bg_ns.name,
            reservation_gen=gen or 0,
        )
    )
    assert reg.get(1, bg_ns.name).status == "idle"
    cli.execute.assert_not_awaited()


async def test_leaf_exception_paths_restore_idle(
    workspace: tuple[DuctorPaths, AgentConfig], tmp_path: Path
) -> None:
    from ductor_bot.background.models import BackgroundTask
    from ductor_bot.orchestrator.flows import named_session_flow, named_session_streaming
    from ductor_bot.session.key import SessionKey

    orch = _orch(workspace)
    manual = NamedSession("manual", 1, "codex", "gpt", "sid", "p", "idle", 1.0)
    orch._named_sessions.add(manual)
    orch._cli_service.execute = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await named_session_flow(orch, SessionKey(chat_id=1), "manual", "prompt")
    assert orch._named_sessions.get(1, "manual").status == "idle"

    streaming = NamedSession("stream", 1, "codex", "gpt", "sid", "p", "idle", 1.0)
    orch._named_sessions.add(streaming)
    orch._cli_service.execute_streaming = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await named_session_streaming(orch, SessionKey(chat_id=1), "stream", "prompt")
    assert orch._named_sessions.get(1, "stream").status == "idle"

    reg = NamedSessionRegistry(tmp_path / "named.json")
    bg_ns = NamedSession("bg", 1, "codex", "gpt", "sid", "p", "idle", 1.0)
    reg.add(bg_ns)
    gen = reg.reserve_followup(1, "bg", "prompt")
    cli = MagicMock()
    cli.execute = AsyncMock(side_effect=RuntimeError("boom"))
    observer = BackgroundObserver(
        tmp_path,
        timeout_seconds=5,
        cli_service=cli,
        named_sessions=reg,
        named_locks=NamedSessionLockPool(reg),
        ia_limiter=IARunningLimiter(16),
    )
    await observer._run_with_session(
        BackgroundTask(
            "t",
            1,
            "prompt",
            1,
            None,
            "codex",
            "gpt",
            0.0,
            session_name="bg",
            reservation_gen=gen or 0,
        )
    )
    assert reg.get(1, "bg").status == "idle"


def test_terminal_ended_is_monotonic_for_updates_and_rollbacks(tmp_path: Path) -> None:
    reg = NamedSessionRegistry(tmp_path / "named.json")
    for name in ("one", "two"):
        ns = NamedSession(name, 1, "codex", "gpt", "sid", "p", "running", 1.0)
        ns.reservation_gen = 1
        reg.add(ns)
    reg.end_session(1, "one")
    assert reg.update_after_response(1, "one", "new") is False
    assert reg.rollback_reservation(1, "one", 1) is False
    assert reg.get(1, "one").status == "ended"
    reg.end_all(1)
    assert reg.update_after_response(1, "two", "new") is False
    assert reg.get(1, "two").status == "ended"


async def test_end_named_session_uses_ns_process_label(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    ns = _ia_session("ia.main.xkill", chat_id=1, status="idle")
    ns.reservation_gen = 3
    orch._named_sessions.add(ns)
    orch._process_registry.kill_by_label = AsyncMock()
    orch._process_registry.clear_label_abort = MagicMock()
    assert await orch.end_named_session(1, ns.name) is True
    orch._process_registry.kill_by_label.assert_not_awaited()
    orch._process_registry.clear_label_abort.assert_not_called()


def test_lock_and_limiter_identity_is_shared_with_background_observer(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    from ductor_bot.cli.codex_cache import CodexModelCache

    orch = _orch(workspace)
    orch._observers.init_task_observers(
        cron_manager=orch._cron_manager,
        webhook_manager=orch._webhook_manager,
        cli_service=orch._cli_service,
        codex_cache=CodexModelCache("", []),
    )
    assert orch._observers.background._named_locks is orch._ns_locks
    assert orch._observers.background._ia_limiter is orch._ia_limiter


async def test_stale_retry_preserves_origin(workspace: tuple[DuctorPaths, AgentConfig]) -> None:
    orch = _orch(workspace)
    orch._cli_service.execute = AsyncMock(
        side_effect=[
            CLIResponse(session_id="", result="invalid session", is_error=True),
            CLIResponse(session_id="sid2", result="ok"),
        ]
    )
    outcome = await orch.handle_interagent_message(
        "main", "prompt", origin=InterAgentOrigin("tg", 777, 88)
    )
    ns = orch._named_sessions.get(12345, outcome.session_name)
    assert ns.ia_chat_id == 777
    assert ns.ia_topic_id == 88


def test_prune_sender_and_global_limits_use_legacy_last_active_fallback(tmp_path: Path) -> None:
    reg = NamedSessionRegistry(tmp_path / "named.json")
    pool = NamedSessionLockPool(reg)
    for i in range(10):
        reg.add(
            _ia_session(f"ia.same{i}.x", sender="same", created_at=float(i), last_active_at=None)
        )
    for i in range(33):
        reg.add(
            _ia_session(f"ia-other-{i}", sender=None, created_at=100.0 + i, last_active_at=None)
        )
    ended = reg.prune_interagent(1, pool)
    assert "ia.same0.x" in ended
    assert "ia.same1.x" in ended
    assert len([s for s in reg.list_active(1) if is_interagent_session(s)]) == 32


def test_recovery_planner_excludes_scoped_interagent_sessions() -> None:
    from ductor_bot.infra.recovery import RecoveryPlanner

    scoped = _ia_session("ia.main.t1.xabc", status="running")
    inflight = MagicMock()
    inflight.load_interrupted.return_value = []
    planner = RecoveryPlanner(inflight=inflight, named_sessions=[scoped], max_age_seconds=9999)
    assert all(a.session_name != scoped.name for a in planner.plan())


def test_i18n_async_notification_has_no_session_placeholder() -> None:
    root = Path(__file__).resolve().parents[1] / "ductor_bot/i18n"
    for locale in ["de", "en", "es", "fr", "id", "nl", "pt", "ru"]:
        line = next(
            line
            for line in (root / locale / "chat.toml").read_text().splitlines()
            if line.startswith("async_task_received")
        )
        assert "{session}" not in line
        assert all(token in line for token in ("{sender}", "{task_id}", "{preview}"))


def test_ask_agent_async_forwards_origin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = (
        Path(__file__).resolve().parents[1]
        / "ductor_bot/_home_defaults/workspace/tools/agent_tools/ask_agent_async.py"
    )
    spec = importlib.util.spec_from_file_location("ask_agent_async_tool", tool)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    captured: dict[str, object] = {}

    class _Resp:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"success": True, "task_id": "t1"}).encode()

    def fake_urlopen(req: object, timeout: int) -> _Resp:
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["ask_agent_async.py", "dev", "hello"])
    monkeypatch.setenv("DUCTOR_AGENT_NAME", "main")
    monkeypatch.setenv("DUCTOR_CHAT_ID", "111")
    monkeypatch.setenv("DUCTOR_TOPIC_ID", "222")
    monkeypatch.setenv("DUCTOR_TRANSPORT", "mx")
    mod.main()
    assert captured["body"]["chat_id"] == 111
    assert captured["body"]["topic_id"] == 222
    assert captured["body"]["transport"] == "mx"


def test_cross_transport_manual_resume_supported_uses_recipient_transport() -> None:
    from ductor_bot.multiagent.bus import InterAgentBus

    bus = InterAgentBus()
    tg_stack = MagicMock()
    tg_stack.config.transport = "telegram"
    tg_stack.config.allowed_user_ids = [1]
    mx_stack = MagicMock()
    mx_stack.config.transport = "matrix"
    mx_stack.config.allowed_user_ids = [1]
    bus.register("tg-recipient", tg_stack)
    bus.register("mx-recipient", mx_stack)
    assert bus._manual_resume_supported("tg-recipient") is True
    assert bus._manual_resume_supported("mx-recipient") is False


async def test_button_callback_style_followup_delegates_without_deadlock(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    orch._named_sessions.add(NamedSession("work", 1, "codex", "gpt", "sid", "p", "idle", 1.0))
    orch._observers.background = MagicMock()
    orch._observers.background.submit.return_value = "task1"
    task_id = await asyncio.wait_for(
        orch.submit_named_followup_bg(1, "work", "prompt", 1, None), timeout=1
    )
    assert task_id == "task1"
    assert orch._named_sessions.get(1, "work").status == "running"


async def test_submit_vs_foreground_contention_rejects_foreground_without_deadlock(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    from ductor_bot.orchestrator.flows import named_session_flow
    from ductor_bot.session.key import SessionKey

    orch = _orch(workspace)
    orch._named_sessions.add(NamedSession("work", 1, "codex", "gpt", "sid", "p", "idle", 1.0))
    assert await orch.reserve_named_followup(1, "work", "bg") == 1
    result = await asyncio.wait_for(
        named_session_flow(orch, SessionKey(chat_id=1), "work", "fg"), timeout=1
    )
    assert "processing" in result.text.lower()


async def test_lock_handoff_prevents_nowait_steal_before_waiter_resumes() -> None:
    pool = NamedSessionLockPool()
    key = (1, "handoff")
    held = pool.try_acquire_nowait(key)
    assert held is not None

    waiter_entered = asyncio.Event()
    waiter_release = asyncio.Event()

    async def wait_for_lock() -> None:
        async with pool.acquire(key):
            waiter_entered.set()
            await waiter_release.wait()

    waiter = asyncio.create_task(wait_for_lock())
    await asyncio.sleep(0)
    assert pool.has_waiters(key)

    await held.__aexit__(None, None, None)
    assert pool.try_acquire_nowait(key) is None
    await asyncio.wait_for(waiter_entered.wait(), timeout=1)
    waiter_release.set()
    await asyncio.wait_for(waiter, timeout=1)


async def test_cancelled_interagent_execution_restores_idle_and_permit(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    entered = asyncio.Event()

    async def block(_request: object) -> CLIResponse:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    orch._cli_service.execute = AsyncMock(side_effect=block)
    task = asyncio.create_task(
        orch.handle_interagent_message(
            "main", "cancel", origin=InterAgentOrigin("tg", 777, 90)
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    sessions = orch._named_sessions.list_active(12345)
    assert len(sessions) == 1
    assert sessions[0].status == "idle"
    assert orch._ia_limiter._running == 0


@pytest.mark.parametrize("termination", ["cancel", "exception", "shutdown"])
async def test_new_background_session_termination_restores_idle(
    tmp_path: Path, termination: str
) -> None:
    from ductor_bot.background.models import BackgroundTask

    reg = NamedSessionRegistry(tmp_path / "named.json")
    ns = reg.create(1, "codex", "gpt", "prompt")
    assert ns.reservation_gen > 0
    locks = NamedSessionLockPool(reg)
    entered = asyncio.Event()

    async def execute(_request: object) -> CLIResponse:
        entered.set()
        if termination == "exception":
            raise RuntimeError("boom")
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    cli = MagicMock()
    cli.execute = AsyncMock(side_effect=execute)
    observer = BackgroundObserver(
        tmp_path,
        timeout_seconds=5,
        cli_service=cli,
        named_sessions=reg,
        named_locks=locks,
        ia_limiter=IARunningLimiter(16),
    )
    results = AsyncMock()
    observer.set_result_handler(results)
    bg_task = BackgroundTask(
        "new-task",
        1,
        "prompt",
        1,
        None,
        "codex",
        "gpt",
        0.0,
        session_name=ns.name,
        reservation_gen=ns.reservation_gen,
    )

    if termination == "exception":
        await observer._run_with_session(bg_task)
    else:
        running = asyncio.create_task(observer._run_with_session(bg_task))
        await asyncio.wait_for(entered.wait(), timeout=1)
        if termination == "shutdown":
            bg_task.asyncio_task = running
            observer._tasks[bg_task.task_id] = bg_task
            await observer.shutdown()
        else:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running

    assert reg.get(1, ns.name).status == "idle"
    results.assert_awaited_once()
    expected_status = "error:internal" if termination == "exception" else "aborted"
    assert results.await_args.args[0].status == expected_status


async def test_session_selector_endall_uses_eviction_contract(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.cli import process_registry as process_registry_module
    from ductor_bot.orchestrator.selectors import session_selector

    orch = _orch(workspace)
    sessions = [
        NamedSession(name, 1, "codex", "gpt", "sid", "p", "running", 1.0)
        for name in ("registered", "late")
    ]
    labels: dict[str, str] = {}
    for ns in sessions:
        orch._named_sessions.add(ns)
        token = orch._named_sessions.begin_execution(1, ns.name)
        labels[ns.name] = f"ns:{ns.name}:{token}"
    processes = {
        name: MagicMock(pid=index, returncode=None, wait=AsyncMock(return_value=0))
        for index, name in enumerate(("registered", "late"), start=1)
    }
    orch._process_registry.register(1, processes["registered"], labels["registered"])
    terminate = MagicMock()
    force = MagicMock()
    monkeypatch.setattr(process_registry_module, "terminate_process_tree", terminate)
    monkeypatch.setattr(process_registry_module, "force_kill_process_tree", force)
    monkeypatch.setattr(process_registry_module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(session_selector, "_build_page", AsyncMock(return_value=MagicMock()))

    await session_selector.handle_session_callback(orch, 1, "nsc:endall")
    orch._process_registry.register(1, processes["late"], labels["late"])
    await orch._process_registry.drain_cleanup_tasks()

    assert all(orch._named_sessions.get(1, ns.name).status == "ended" for ns in sessions)
    assert {call.args[0] for call in terminate.call_args_list} == {1, 2}
    assert {call.args[0] for call in force.call_args_list} == {1, 2}
    assert all(process.wait.await_count >= 1 for process in processes.values())
    assert orch._process_registry.has_active(1) is False


def test_new_named_session_resolver_failure_ends_reservation(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.cli import param_resolver

    orch = _orch(workspace)
    orch._observers.background = MagicMock()
    monkeypatch.setattr(
        param_resolver, "resolve_cli_config", MagicMock(side_effect=RuntimeError("resolve"))
    )

    with pytest.raises(RuntimeError, match="resolve"):
        orch.submit_named_session(1, "prompt", NamedSessionRequest(1, None))

    sessions = list(orch._named_sessions._sessions.values())
    assert len(sessions) == 1
    assert sessions[0].status == "ended"
    assert sessions[0].reservation_gen == 1
    orch._observers.background.submit.assert_not_called()


async def test_new_named_session_task_limit_failure_ends_reservation(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.background.observer import MAX_TASKS_PER_CHAT
    from ductor_bot.cli import param_resolver

    paths, _ = workspace
    orch = _orch(workspace)
    observer = BackgroundObserver(paths, timeout_seconds=5)
    release = asyncio.Event()
    pending: list[asyncio.Task[None]] = []

    async def wait_for_release() -> None:
        await release.wait()

    for index in range(MAX_TASKS_PER_CHAT):
        task = asyncio.create_task(wait_for_release())
        pending.append(task)
        observer._tasks[str(index)] = BackgroundTask(
            str(index),
            1,
            "prompt",
            index,
            None,
            "codex",
            "gpt",
            0.0,
            asyncio_task=task,
        )
    orch._observers.background = observer
    monkeypatch.setattr(param_resolver, "resolve_cli_config", MagicMock())

    try:
        with pytest.raises(ValueError, match="Too many background tasks"):
            orch.submit_named_session(1, "prompt", NamedSessionRequest(1, None))
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    sessions = list(orch._named_sessions._sessions.values())
    assert len(sessions) == 1
    assert sessions[0].status == "ended"
    assert sessions[0].reservation_gen == 1


async def test_reserved_followup_cannot_be_overtaken_or_provider_switched(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    ns = _ia_session("ia.main.t1.xreserved", chat_id=12345, status="idle", topic_id=1)
    orch._named_sessions.add(ns)
    generation = await orch.reserve_named_followup(12345, ns.name, "reserved prompt")

    direct = await orch.handle_interagent_message(
        "main", "direct prompt", origin=InterAgentOrigin("tg", 777, 1)
    )

    assert direct.ok is False
    assert direct.error_kind == "busy"
    assert orch._named_sessions.get(12345, ns.name) is ns
    assert ns.status == "running"
    assert (ns.provider, ns.model) == ("codex", "gpt-old")
    orch._cli_service.execute.assert_not_awaited()

    observer = BackgroundObserver(
        orch.paths,
        timeout_seconds=5,
        cli_service=orch._cli_service,
        named_sessions=orch._named_sessions,
        named_locks=orch._ns_locks,
        ia_limiter=orch._ia_limiter,
    )
    await observer._run_with_session(
        BackgroundTask(
            "reserved-task",
            12345,
            "reserved prompt",
            1,
            1,
            ns.provider,
            ns.model,
            0.0,
            session_name=ns.name,
            reservation_gen=generation,
            transport=ns.transport,
        )
    )

    orch._cli_service.execute.assert_awaited_once()
    assert orch._cli_service.execute.await_args.args[0].prompt == "reserved prompt"
    assert ns.status == "idle"


async def test_background_ceiling_delivers_terminal_error_and_rolls_back(
    tmp_path: Path,
) -> None:
    reg = NamedSessionRegistry(tmp_path / "named.json")
    ns = _ia_session("ia.main.xceiling", status="idle")
    reg.add(ns)
    generation = reg.reserve_followup(1, ns.name, "prompt")
    results = AsyncMock()
    cli = MagicMock()
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="sid", result="unused"))
    observer = BackgroundObserver(
        tmp_path,
        timeout_seconds=5,
        cli_service=cli,
        named_sessions=reg,
        named_locks=NamedSessionLockPool(reg),
        ia_limiter=IARunningLimiter(0),
    )
    observer.set_result_handler(results)

    await observer._run_with_session(
        BackgroundTask(
            "ceiling-task",
            1,
            "prompt",
            1,
            None,
            "codex",
            "gpt",
            0.0,
            session_name=ns.name,
            reservation_gen=generation or 0,
        )
    )

    cli.execute.assert_not_awaited()
    results.assert_awaited_once()
    delivered = results.await_args.args[0]
    assert delivered.status == "error:ceiling"
    assert "retry" in delivered.result_text.lower()
    assert reg.get(1, ns.name).status == "idle"


async def test_named_background_recovery_preserves_reloaded_topic_and_transport(
    workspace: tuple[DuctorPaths, AgentConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ductor_bot.messenger.telegram import startup

    path = tmp_path / "named.json"
    reg = NamedSessionRegistry(path)
    sessions: list[NamedSession] = []
    for topic_id, transport in ((10, "tg"), (20, "mx")):
        ns = reg.create(
            1,
            "codex",
            "gpt",
            f"topic {topic_id}",
            transport=transport,
            topic_id=topic_id,
        )
        ns.session_id = f"sid-{topic_id}"
        reg.mark_running(1, ns.name, f"topic {topic_id}")
        sessions.append(ns)

    reloaded = NamedSessionRegistry(path)
    orch = _orch(workspace)
    orch._named_sessions = reloaded
    orch._ns_locks = NamedSessionLockPool(reloaded)
    cli = orch._cli_service
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="sid", result="ok"))
    observer = BackgroundObserver(
        orch.paths,
        timeout_seconds=5,
        cli_service=cli,
        named_sessions=reloaded,
        named_locks=orch._ns_locks,
        ia_limiter=IARunningLimiter(16),
    )
    orch._observers.background = observer
    bot = MagicMock()
    bot._orch = orch
    bot.config = orch.config
    bot.notification_service.notify = AsyncMock()
    monkeypatch.setattr(startup, "consume_upgrade_sentinel", lambda _path: None)

    await startup._handle_recovery(bot, {})
    await asyncio.gather(
        *(task.asyncio_task for task in observer._tasks.values() if task.asyncio_task)
    )
    await observer.shutdown()

    requests = [call.args[0] for call in cli.execute.await_args_list]
    assert [request.topic_id for request in requests] == [10, 20]
    assert [request.transport for request in requests] == ["tg", "mx"]
    assert reloaded.get(1, sessions[1].name).last_topic_id == 20

    nested = [
        await orch.handle_interagent_message(
            "main",
            "nested",
            origin=InterAgentOrigin(request.transport, 1, request.topic_id),
        )
        for request in requests
    ]
    assert nested[0].session_name != nested[1].session_name


def test_prune_physically_removes_exact_per_sender_excess(tmp_path: Path) -> None:
    path = tmp_path / "named.json"
    reg = NamedSessionRegistry(path)
    pool = NamedSessionLockPool(reg)
    for index in range(9):
        reg.add(
            _ia_session(
                f"ia.sender-a.x{index}",
                sender="sender-a",
                topic_id=index,
                created_at=float(index),
                last_active_at=float(index),
            )
        )
    for index in range(24):
        reg.add(
            _ia_session(
                f"ia.sender-{index}.xother",
                sender=f"sender-{index}",
                topic_id=index,
                created_at=float(index + 20),
                last_active_at=float(index + 20),
            )
        )

    ended = reg.prune_interagent(1, pool)

    assert ended == ["ia.sender-a.x0"]
    assert len(reg._sessions) == 32
    assert sum(ns.ia_sender == "sender-a" for ns in reg._sessions.values()) == 8
    assert reg.prune_interagent(1, pool) == []
    reloaded = NamedSessionRegistry(path)
    assert len(reloaded._sessions) == 32


@pytest.mark.parametrize(
    ("transports", "expected"),
    [
        (["telegram", "matrix"], True),
        (["matrix", "telegram"], True),
        (["matrix"], False),
    ],
)
def test_manual_resume_supported_checks_all_transports(
    transports: list[str], expected: bool
) -> None:
    from ductor_bot.multiagent.bus import InterAgentBus

    bus = InterAgentBus()
    stack = MagicMock()
    stack.config.transports = transports
    stack.config.transport = transports[0]
    stack.config.allowed_user_ids = [1]
    bus.register("recipient", stack)

    assert bus._manual_resume_supported("recipient") is expected


async def test_stale_retry_exception_is_typed_execution_failure(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    orch._cli_service.execute = AsyncMock(
        side_effect=[
            CLIResponse(session_id="", result="invalid session", is_error=True),
            RuntimeError("retry failed"),
        ]
    )

    outcome = await orch.handle_interagent_message("main", "prompt")

    assert outcome.ok is False
    assert outcome.error_kind == "execution"
    assert "after stale-session retry" in outcome.text


async def test_cli_error_response_is_typed_failure(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    orch._cli_service.execute = AsyncMock(
        return_value=CLIResponse(session_id="sid", result="provider error", is_error=True)
    )

    outcome = await orch.handle_interagent_message("main", "prompt")

    assert outcome.ok is False
    assert outcome.error_kind == "cli"
    assert outcome.text == "provider error"


def test_async_failure_uses_error_delivery_without_parent_injection() -> None:
    from ductor_bot.bus.adapters import (
        build_interagent_injection_prompt,
        from_interagent_result,
    )

    result = AsyncInterAgentResult(
        task_id="failed-task",
        sender="main",
        recipient="worker",
        message_preview="prompt",
        result_text="",
        success=False,
        error="execution failed",
        chat_id=1,
    )

    prompt = build_interagent_injection_prompt(
        result, agent_name="main", transport_label="Telegram chat"
    )
    envelope = from_interagent_result(result, 1, injection_prompt=prompt)

    assert prompt == ""
    assert envelope.is_error is True
    assert envelope.needs_injection is False
    assert envelope.status == "error"


async def _assert_prestart_cancel_delivers_once(tmp_path: Path, action: str) -> None:
    reg = NamedSessionRegistry(tmp_path / f"{action}.json")
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "idle", 1.0)
    reg.add(ns)
    generation = reg.reserve_followup(1, ns.name, "prompt")
    cli = MagicMock()
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="sid", result="unused"))
    observer = BackgroundObserver(
        tmp_path,
        timeout_seconds=5,
        cli_service=cli,
        named_sessions=reg,
        named_locks=NamedSessionLockPool(reg),
        ia_limiter=IARunningLimiter(16),
    )
    results = AsyncMock()
    observer.set_result_handler(results)
    config = TaskExecutionConfig(
        provider="codex",
        model="gpt",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="default",
        working_dir=str(tmp_path),
        file_access="workspace",
    )
    observer.submit(
        BackgroundSubmit(
            chat_id=1,
            prompt="prompt",
            message_id=1,
            thread_id=None,
            session_name=ns.name,
            resume_session_id=ns.session_id,
            provider_override=ns.provider,
            model_override=ns.model,
            reservation_gen=generation or 0,
        ),
        config,
    )

    if action == "cancel_all":
        assert await observer.cancel_all(1) == 1
    else:
        await observer.shutdown()

    cli.execute.assert_not_awaited()
    results.assert_awaited_once()
    assert results.await_args.args[0].status == "aborted"
    assert reg.get(1, ns.name).status == "idle"


async def test_prestart_cancel_all_delivers_single_aborted_result(tmp_path: Path) -> None:
    await _assert_prestart_cancel_delivers_once(tmp_path, "cancel_all")


async def test_prestart_shutdown_delivers_single_aborted_result(tmp_path: Path) -> None:
    await _assert_prestart_cancel_delivers_once(tmp_path, "shutdown")


async def test_background_result_transport_routes_only_to_matrix(tmp_path: Path) -> None:
    from ductor_bot.bus.adapters import from_background_result
    from ductor_bot.bus.bus import MessageBus

    class Adapter:
        def __init__(self, name: str) -> None:
            self.transport_name = name
            self.deliver = AsyncMock()
            self.deliver_broadcast = AsyncMock()

    matrix = Adapter("mx")
    telegram = Adapter("tg")
    bus = MessageBus()
    bus.register_transport(matrix)
    bus.register_transport(telegram)
    delivered: list[BackgroundResult] = []

    async def on_result(result: BackgroundResult) -> None:
        delivered.append(result)
        await bus.submit(from_background_result(result))

    cli = MagicMock()
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="sid", result="done"))
    observer = BackgroundObserver(tmp_path, timeout_seconds=5, cli_service=cli)
    observer.set_result_handler(on_result)
    await observer._run_with_session(
        BackgroundTask(
            "mx-task",
            1,
            "prompt",
            1,
            20,
            "codex",
            "gpt",
            0.0,
            session_name="work",
            transport="mx",
        )
    )

    matrix.deliver.assert_awaited_once()
    telegram.deliver.assert_not_awaited()
    telegram.deliver_broadcast.assert_not_awaited()
    assert matrix.deliver.await_args.args[0].transport == "mx"

    matrix_only = Adapter("mx")
    matrix_bus = MessageBus()
    matrix_bus.register_transport(matrix_only)
    await matrix_bus.submit(from_background_result(delivered[0]))
    matrix_only.deliver.assert_awaited_once()
    matrix_only.deliver_broadcast.assert_not_awaited()


@pytest.mark.parametrize("action", ["cancel_all", "shutdown"])
async def test_delivery_in_progress_survives_background_cancellation(
    tmp_path: Path, action: str
) -> None:
    reg = NamedSessionRegistry(tmp_path / f"delivery-{action}.json")
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "idle", 1.0)
    reg.add(ns)
    generation = reg.reserve_followup(1, ns.name, "prompt")
    cli = MagicMock()
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="sid", result="done"))
    observer = BackgroundObserver(
        tmp_path,
        timeout_seconds=5,
        cli_service=cli,
        named_sessions=reg,
        named_locks=NamedSessionLockPool(reg),
        ia_limiter=IARunningLimiter(16),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def blocking_handler(_result: BackgroundResult) -> None:
        entered.set()
        await release.wait()
        completed.set()

    handler = AsyncMock(side_effect=blocking_handler)
    observer.set_result_handler(handler)
    config = TaskExecutionConfig(
        provider="codex",
        model="gpt",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="default",
        working_dir=str(tmp_path),
        file_access="workspace",
    )
    observer.submit(
        BackgroundSubmit(
            chat_id=1,
            prompt="prompt",
            message_id=1,
            thread_id=None,
            session_name=ns.name,
            resume_session_id=ns.session_id,
            provider_override=ns.provider,
            model_override=ns.model,
            reservation_gen=generation or 0,
        ),
        config,
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    cancellation = asyncio.create_task(
        observer.cancel_all(1) if action == "cancel_all" else observer.shutdown()
    )
    await asyncio.sleep(0)
    assert not cancellation.done()
    release.set()
    await asyncio.wait_for(cancellation, timeout=1)

    assert completed.is_set()
    handler.assert_awaited_once()
    assert reg.get(1, ns.name).status == "idle"


async def test_superseded_background_result_is_failure_for_both_formatters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ductor_bot.bus.adapters import from_background_result
    from ductor_bot.messenger.matrix import transport as matrix_module
    from ductor_bot.messenger.matrix.transport import MatrixTransport
    from ductor_bot.messenger.telegram.transport import TelegramTransport

    result = BackgroundResult(
        task_id="stale",
        chat_id=1,
        message_id=1,
        thread_id=None,
        prompt_preview="prompt",
        result_text="superseded",
        status="error:superseded",
        elapsed_seconds=0.1,
        provider="codex",
        model="gpt",
        session_name="work",
    )
    envelope = from_background_result(result)
    assert envelope.is_error is True
    telegram_text = TelegramTransport._format_named_session(envelope, "0s")
    assert "Failed" in telegram_text
    assert "Complete" not in telegram_text

    bot = MagicMock()
    bot.id_map.int_to_room.return_value = "!room:example"
    bot.orchestrator = None
    matrix_send = AsyncMock()
    monkeypatch.setattr(matrix_module, "matrix_send_rich", matrix_send)
    await MatrixTransport(bot).deliver(envelope)
    matrix_text = matrix_send.await_args.args[2]
    assert "Failed" in matrix_text
    assert "Complete" not in matrix_text


def test_resume_hint_uses_recipient_transport_and_sender_delivery_label() -> None:
    from ductor_bot.bus.adapters import build_interagent_injection_prompt

    result = AsyncInterAgentResult(
        task_id="t1",
        sender="main",
        recipient="dev",
        message_preview="prompt",
        result_text="done",
        session_name="ia-main",
        transport="mx",
        manual_resume_supported=True,
        manual_resume_transport="tg",
    )
    prompt = build_interagent_injection_prompt(
        result,
        agent_name="main",
        transport_label="Matrix room",
    )
    assert "recipient's Telegram chat" in prompt
    assert "recipient's Matrix room" not in prompt
    assert "in your Matrix room" in prompt


def test_named_session_request_positional_provider_model_compatibility() -> None:
    request = NamedSessionRequest(1, None, "codex", "gpt")
    assert request.provider_override == "codex"
    assert request.model_override == "gpt"
    assert request.transport == "tg"


async def test_end_before_process_register_terminates_late_process_and_aborts_result(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.cli import process_registry as process_registry_module

    orch = _orch(workspace)
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "idle", 1.0)
    ns.reservation_gen = 1
    orch._named_sessions.add(ns)
    generation = orch._named_sessions.reserve_followup(1, ns.name, "prompt")
    assert generation == 2
    entered = asyncio.Event()
    release = asyncio.Event()
    process = MagicMock()
    process.pid = 4242
    process.returncode = None
    process.wait = AsyncMock(return_value=0)
    terminate = MagicMock()
    force = MagicMock()
    monkeypatch.setattr(process_registry_module, "terminate_process_tree", terminate)
    monkeypatch.setattr(process_registry_module, "force_kill_process_tree", force)
    monkeypatch.setattr(process_registry_module.asyncio, "sleep", AsyncMock())

    async def execute(request: object) -> CLIResponse:
        entered.set()
        await release.wait()
        tracked = orch._process_registry.register(
            1,
            process,
            request.process_label,
            topic_id=request.topic_id,
        )
        await orch._process_registry.drain_cleanup_tasks()
        orch._process_registry.unregister(tracked)
        return CLIResponse(session_id="sid", result="should not deliver")

    orch._cli_service.execute = AsyncMock(side_effect=execute)
    results = AsyncMock()
    observer = BackgroundObserver(
        orch.paths,
        timeout_seconds=5,
        cli_service=orch._cli_service,
        named_sessions=orch._named_sessions,
        named_locks=orch._ns_locks,
        ia_limiter=orch._ia_limiter,
        process_registry=orch._process_registry,
    )
    observer.set_result_handler(results)
    bg_task = BackgroundTask(
        "race",
        1,
        "prompt",
        1,
        10,
        "codex",
        "gpt",
        0.0,
        session_name=ns.name,
        reservation_gen=generation,
    )

    runner = asyncio.create_task(observer._run_with_session(bg_task))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert await orch.end_named_session(1, ns.name) is True
    release.set()
    await asyncio.wait_for(runner, timeout=1)

    terminate.assert_called_once_with(4242)
    force.assert_called_once_with(4242)
    process.wait.assert_awaited()
    results.assert_awaited_once()
    assert results.await_args.args[0].status == "aborted"
    assert "should not deliver" not in results.await_args.args[0].result_text
    assert orch._named_sessions.get(1, ns.name).status == "ended"
    assert orch._process_registry.has_active(1) is False
    assert not orch._process_registry._aborted_labels


async def test_execution_abort_remains_sticky_across_two_sequential_processes(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.cli import process_registry as process_registry_module

    orch = _orch(workspace)
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "running", 1.0)
    orch._named_sessions.add(ns)
    token = orch._named_sessions.begin_execution(1, ns.name)
    label = f"ns:{ns.name}:{token}"
    terminate = MagicMock()
    force = MagicMock()
    monkeypatch.setattr(process_registry_module, "terminate_process_tree", terminate)
    monkeypatch.setattr(process_registry_module, "force_kill_process_tree", force)
    monkeypatch.setattr(process_registry_module.asyncio, "sleep", AsyncMock())
    assert await orch.end_named_session(1, ns.name) is True

    processes = [
        MagicMock(pid=pid, returncode=None, wait=AsyncMock(return_value=0)) for pid in (1, 2)
    ]
    orch._process_registry.register(1, processes[0], label)
    await orch._process_registry.drain_cleanup_tasks()
    assert orch._process_registry.was_aborted_label(1, label) is True
    orch._process_registry.register(1, processes[1], label)
    await orch._process_registry.drain_cleanup_tasks()

    assert [call.args[0] for call in terminate.call_args_list] == [1, 2]
    assert [call.args[0] for call in force.call_args_list] == [1, 2]
    assert all(process.wait.await_count >= 1 for process in processes)
    orch._process_registry.clear_label_abort(1, label)
    orch._named_sessions.finish_execution(1, ns.name, token)
    assert orch._process_registry.was_aborted_label(1, label) is False


async def test_end_named_session_kills_already_registered_generation(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.cli import process_registry as process_registry_module

    orch = _orch(workspace)
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "running", 1.0)
    ns.reservation_gen = 4
    orch._named_sessions.add(ns)
    token = orch._named_sessions.begin_execution(1, ns.name)
    label = f"ns:{ns.name}:{token}"
    process = MagicMock(pid=44, returncode=None)
    tracked = orch._process_registry.register(1, process, label)
    kill = AsyncMock(return_value=1)
    monkeypatch.setattr(process_registry_module, "_kill_processes", kill)

    assert await orch.end_named_session(1, ns.name) is True
    kill.assert_awaited_once()
    assert orch._process_registry.has_active(1) is False
    assert orch._process_registry.was_aborted_label(1, label) is True
    orch._process_registry.unregister(tracked)
    assert orch._process_registry.was_aborted_label(1, label) is True
    orch._process_registry.clear_label_abort(1, label)
    orch._named_sessions.finish_execution(1, ns.name, token)
    assert orch._process_registry.was_aborted_label(1, label) is False


async def test_idle_interagent_end_does_not_poison_same_identity_recreation(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    orch._cli_service.execute = AsyncMock(
        side_effect=[
            CLIResponse(session_id="sid-1", result="first"),
            CLIResponse(session_id="sid-2", result="second"),
        ]
    )
    origin = InterAgentOrigin("tg", 777, 10)

    first = await orch.handle_interagent_message("main", "one", origin=origin)
    assert first.ok is True
    assert await orch.end_named_session(12345, first.session_name) is True
    assert not orch._process_registry._aborted_labels
    second = await orch.handle_interagent_message("main", "two", origin=origin)

    assert second.ok is True
    assert second.text == "second"
    labels = [call.args[0].process_label for call in orch._cli_service.execute.await_args_list]
    assert labels[0] != labels[1]
    assert all(label.startswith(f"ns:{first.session_name}:") for label in labels)


async def test_topic_abort_excludes_generation_scoped_named_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ductor_bot.cli import process_registry as process_registry_module
    from ductor_bot.cli.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    named = MagicMock(pid=1, returncode=None)
    foreground = MagicMock(pid=2, returncode=None)
    registry.register(1, named, "ns:work:7", topic_id=10)
    registry.register(1, foreground, "main", topic_id=10)
    kill = AsyncMock(return_value=1)
    monkeypatch.setattr(process_registry_module, "_kill_processes", kill)

    assert await registry.kill_by_chat_topic(1, 10) == 1
    assert registry.has_active(1) is True
    assert registry._processes[1][0].label == "ns:work:7"


@pytest.mark.parametrize("action", ["cancel_all", "shutdown"])
async def test_stateless_prestart_cancel_delivers_one_aborted_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from ductor_bot.background import observer as observer_module

    runner = AsyncMock()
    monkeypatch.setattr(observer_module, "run_oneshot_task", runner)
    observer = BackgroundObserver(tmp_path, timeout_seconds=5)
    results = AsyncMock()
    observer.set_result_handler(results)
    config = TaskExecutionConfig(
        provider="codex",
        model="gpt",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="default",
        working_dir=str(tmp_path),
        file_access="workspace",
    )
    observer.submit(
        BackgroundSubmit(
            chat_id=1,
            prompt="stateless",
            message_id=1,
            thread_id=None,
        ),
        config,
    )

    if action == "cancel_all":
        assert await observer.cancel_all(1) == 1
    else:
        await observer.shutdown()

    runner.assert_not_awaited()
    results.assert_awaited_once()
    delivered = results.await_args.args[0]
    assert delivered.status == "aborted"
    assert delivered.session_name == ""


async def test_abort_sets_sticky_barrier_for_unregistered_named_execution(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.cli import process_registry as process_registry_module

    orch = _orch(workspace)
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "running", 1.0)
    orch._named_sessions.add(ns)
    token = orch._named_sessions.begin_execution(1, ns.name)
    label = f"ns:{ns.name}:{token}"
    process = MagicMock(pid=91, returncode=None, wait=AsyncMock(return_value=0))
    terminate = MagicMock()
    force = MagicMock()
    monkeypatch.setattr(process_registry_module, "terminate_process_tree", terminate)
    monkeypatch.setattr(process_registry_module, "force_kill_process_tree", force)
    monkeypatch.setattr(process_registry_module.asyncio, "sleep", AsyncMock())

    assert await orch.abort(1) == 0
    assert orch._process_registry.was_aborted_label(1, label) is True
    orch._process_registry.register(1, process, label)
    await orch._process_registry.drain_cleanup_tasks()

    terminate.assert_called_once_with(91)
    force.assert_called_once_with(91)
    process.wait.assert_awaited()
    assert orch._process_registry.has_active(1) is False


async def test_abort_skips_barrier_when_execution_finishes_during_kill_all(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "running", 1.0)
    orch._named_sessions.add(ns)
    token = orch._named_sessions.begin_execution(1, ns.name)

    async def finish_execution(_chat_id: int) -> int:
        orch._named_sessions.finish_execution(1, ns.name, token)
        return 0

    orch._process_registry.kill_all = AsyncMock(side_effect=finish_execution)

    assert await orch.abort(1) == 0
    assert not orch._process_registry._aborted_labels


async def test_abort_clears_barrier_when_execution_finishes_before_label_kill(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)
    ns = NamedSession("work", 1, "codex", "gpt", "sid", "p", "running", 1.0)
    orch._named_sessions.add(ns)
    token = orch._named_sessions.begin_execution(1, ns.name)

    async def schedule_finish(_chat_id: int) -> int:
        asyncio.get_running_loop().call_soon(
            orch._named_sessions.finish_execution, 1, ns.name, token
        )
        return 0

    orch._process_registry.kill_all = AsyncMock(side_effect=schedule_finish)

    assert await orch.abort(1) == 0
    assert not orch._process_registry._aborted_labels


async def test_repeated_abort_races_do_not_accumulate_markers(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _orch(workspace)

    for index in range(3):
        ns = NamedSession(
            f"work-{index}", 1, "codex", "gpt", "sid", "p", "running", 1.0
        )
        orch._named_sessions.add(ns)
        token = orch._named_sessions.begin_execution(1, ns.name)

        async def schedule_finish(
            _chat_id: int, *, name: str = ns.name, execution_token: str = token
        ) -> int:
            asyncio.get_running_loop().call_soon(
                orch._named_sessions.finish_execution, 1, name, execution_token
            )
            return 0

        orch._process_registry.kill_all = AsyncMock(side_effect=schedule_finish)
        assert await orch.abort(1) == 0
        assert not orch._process_registry._aborted_labels


async def test_shutdown_waits_for_late_process_cleanup(
    workspace: tuple[DuctorPaths, AgentConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ductor_bot.cli import process_registry as process_registry_module
    from ductor_bot.orchestrator import lifecycle

    orch = _orch(workspace)
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    observers_stopped = asyncio.Event()

    async def controlled_cleanup(_entries: object) -> int:
        entered.set()
        await release.wait()
        completed.set()
        return 1

    monkeypatch.setattr(process_registry_module, "_kill_processes", controlled_cleanup)
    monkeypatch.setattr(lifecycle, "cleanup_ductor_links", MagicMock())
    await orch._process_registry.kill_by_label(1, "ns:work:token")
    process = MagicMock(pid=92, returncode=None)
    orch._process_registry.register(1, process, "ns:work:token")
    await asyncio.wait_for(entered.wait(), timeout=1)
    orch._process_registry.kill_all_active = AsyncMock(return_value=0)

    async def stop_observers() -> None:
        observers_stopped.set()

    orch._observers.stop_all = AsyncMock(side_effect=stop_observers)

    stopping = asyncio.create_task(lifecycle.shutdown(orch))
    await asyncio.wait_for(observers_stopped.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await asyncio.wait_for(stopping, timeout=1)

    assert completed.is_set()
    assert not orch._process_registry._cleanup_tasks
