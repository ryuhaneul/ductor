"""Tests for orchestrator inter-agent Named Session handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
def orch_ia(workspace: tuple[DuctorPaths, AgentConfig]) -> Orchestrator:
    """Orchestrator with mocked CLIService for inter-agent tests."""
    paths, config = workspace
    config.allowed_user_ids = [12345]
    o = Orchestrator(config, paths, agent_name="codex")
    mock_cli = MagicMock()
    mock_cli._config = MagicMock()
    mock_cli._config.agent_name = "codex"
    mock_cli._config.cli_timeout = 120
    mock_cli.execute = AsyncMock(return_value=CLIResponse(session_id="sess-001", result="done"))
    object.__setattr__(o, "_cli_service", mock_cli)
    return o


class TestInteragentChatId:
    """Test _interagent_chat_id helper."""

    def test_returns_first_allowed_user(self, orch_ia: Orchestrator) -> None:
        assert orch_ia._interagent_chat_id() == 12345

    def test_returns_zero_when_no_users(self, workspace: tuple[DuctorPaths, AgentConfig]) -> None:
        paths, config = workspace
        config.allowed_user_ids = []
        o = Orchestrator(config, paths)
        assert o._interagent_chat_id() == 0


class TestGetOrCreateInteragentSession:
    """Test _get_or_create_interagent_session."""

    def test_creates_new_session(self, orch_ia: Orchestrator) -> None:
        ns, is_new, notice = orch_ia._get_or_create_interagent_session("main")
        assert is_new is True
        assert notice == ""
        assert ns.name == "ia-main"
        assert ns.chat_id == 12345
        assert ns.status == "running"

    def test_reuses_existing_session(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = orch_ia._get_or_create_interagent_session("main")
        ns1.status = "idle"
        ns2, is_new2, notice = orch_ia._get_or_create_interagent_session("main")
        assert is_new2 is False
        assert notice == ""
        assert ns2.name == ns1.name

    def test_new_session_flag_resets_existing(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = orch_ia._get_or_create_interagent_session("main")
        ns1.status = "idle"
        ns1.session_id = "old-session"

        ns2, is_new, _ = orch_ia._get_or_create_interagent_session("main", new_session=True)
        assert is_new is True
        assert ns2.session_id == ""  # Fresh session, no resume ID

    def test_different_senders_get_different_sessions(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = orch_ia._get_or_create_interagent_session("alice")
        ns2, _, _ = orch_ia._get_or_create_interagent_session("bob")
        assert ns1.name == "ia-alice"
        assert ns2.name == "ia-bob"
        assert ns1.name != ns2.name

    def test_ended_session_creates_new_one(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = orch_ia._get_or_create_interagent_session("main")
        ns1.status = "ended"

        ns2, is_new, _ = orch_ia._get_or_create_interagent_session("main")
        assert is_new is True
        assert ns2.session_id == ""

    def test_provider_switch_resets_session(self, orch_ia: Orchestrator) -> None:
        # Start with a codex model so the session is created for provider "codex"
        orch_ia._config.model = "gpt-5.3-codex"
        ns1, _, notice1 = orch_ia._get_or_create_interagent_session("main")
        assert notice1 == ""
        assert ns1.provider == "codex"
        ns1.status = "idle"
        ns1.session_id = "codex-sess-1"

        # Switch to a claude model → different provider
        orch_ia._config.model = "sonnet"

        ns2, is_new, notice2 = orch_ia._get_or_create_interagent_session("main")
        assert is_new is True
        assert ns2.session_id == ""  # Fresh — old codex session discarded
        assert "provider" in notice2.lower()
        assert ns2.provider == "claude"

    def test_same_provider_no_notice(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = orch_ia._get_or_create_interagent_session("main")
        ns1.status = "idle"

        ns2, is_new, notice = orch_ia._get_or_create_interagent_session("main")
        assert is_new is False
        assert notice == ""


class TestHandleInteragentMessage:
    """Test handle_interagent_message."""

    async def test_returns_result_and_session_name(self, orch_ia: Orchestrator) -> None:
        result_text, session_name, notice = await orch_ia.handle_interagent_message(
            "main", "Do something"
        )
        assert result_text == "done"
        assert session_name == "ia-main"
        assert notice == ""

    async def test_creates_named_session(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        assert ns.session_id == "sess-001"
        assert ns.status == "idle"

    async def test_resumes_existing_session(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")

        # Second call should resume with the session_id
        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="sess-002", result="continued")
        )
        result_text, _, _ = await orch_ia.handle_interagent_message("main", "Task 2")
        assert result_text == "continued"

        # Verify resume_session was passed
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.resume_session == "sess-001"

    async def test_new_session_flag_starts_fresh(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")

        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="sess-new", result="fresh start")
        )
        result_text, _, _ = await orch_ia.handle_interagent_message(
            "main", "New task", new_session=True
        )
        assert result_text == "fresh start"

        # Verify resume_session is None (fresh session)
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.resume_session is None

    async def test_prompt_contains_interagent_markers(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Hello world")
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert "[INTER-AGENT MESSAGE from 'main'" in request.prompt
        assert "Hello world" in request.prompt
        assert "[END INTER-AGENT MESSAGE]" in request.prompt

    async def test_process_label_set_correctly(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Test")
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.process_label == "interagent:main"

    async def test_error_returns_error_text(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))
        result_text, session_name, _ = await orch_ia.handle_interagent_message("main", "Crash")
        assert "Error" in result_text
        assert session_name == "ia-main"

    async def test_provider_switch_returns_notice(self, orch_ia: Orchestrator) -> None:
        # Start with codex provider
        orch_ia._config.model = "gpt-5.3-codex"
        await orch_ia.handle_interagent_message("main", "Task 1")

        # Switch to claude provider
        orch_ia._config.model = "sonnet"
        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="claude-sess", result="switched")
        )
        result_text, _, notice = await orch_ia.handle_interagent_message("main", "Task 2")
        assert result_text == "switched"
        assert "provider" in notice.lower()
        # Fresh session → no resume
        call_args = orch_ia._cli_service.execute.call_args
        assert call_args[0][0].resume_session is None

    async def test_session_idle_after_error(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))
        await orch_ia.handle_interagent_message("main", "Crash")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        assert ns.status == "idle"


class TestHandleAsyncInteragentResult:
    """Test handle_async_interagent_result."""

    async def test_basic_result_processing(self, orch_ia: Orchestrator) -> None:
        result = await orch_ia.handle_async_interagent_result(
            "Task completed successfully",
            recipient="helper",
            task_id="task-001",
            chat_id=12345,
        )
        assert result == "done"

    async def test_prompt_contains_session_hint(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_async_interagent_result(
            "Result",
            recipient="helper",
            task_id="task-001",
            chat_id=12345,
            session_name="ia-codex",
        )
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert "ia-codex" in request.prompt
        assert "@ia-codex" in request.prompt

    async def test_prompt_without_session_name(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_async_interagent_result(
            "Result",
            recipient="helper",
            task_id="task-001",
            chat_id=12345,
            session_name="",
        )
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert "@" not in request.prompt or "ia-" not in request.prompt

    async def test_error_handling(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("oops"))
        result = await orch_ia.handle_async_interagent_result(
            "Result",
            recipient="helper",
            task_id="task-001",
        )
        assert "Error" in result
