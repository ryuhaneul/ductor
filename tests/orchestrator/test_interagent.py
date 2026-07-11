"""Tests for orchestrator inter-agent Named Session handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.injection import (
    _get_or_create_interagent_session,
    _interagent_chat_id,
)
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
        assert _interagent_chat_id(orch_ia) == 12345

    def test_returns_zero_when_no_users(self, workspace: tuple[DuctorPaths, AgentConfig]) -> None:
        paths, config = workspace
        config.allowed_user_ids = []
        o = Orchestrator(config, paths)
        assert _interagent_chat_id(o) == 0


class TestGetOrCreateInteragentSession:
    """Test _get_or_create_interagent_session."""

    def test_creates_new_session(self, orch_ia: Orchestrator) -> None:
        ns, is_new, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert notice == ""
        assert ns.name == "ia-main"
        assert ns.chat_id == 12345
        assert ns.status == "running"

    def test_reuses_existing_session(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"
        ns2, is_new2, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new2 is False
        assert notice == ""
        assert ns2.name == ns1.name

    def test_new_session_flag_resets_existing(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"
        ns1.session_id = "old-session"

        ns2, is_new, _ = _get_or_create_interagent_session(orch_ia, "main", new_session=True)
        assert is_new is True
        assert ns2.session_id == ""  # Fresh session, no resume ID

    def test_different_senders_get_different_sessions(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "alice")
        ns2, _, _ = _get_or_create_interagent_session(orch_ia, "bob")
        assert ns1.name == "ia-alice"
        assert ns2.name == "ia-bob"
        assert ns1.name != ns2.name

    def test_ended_session_creates_new_one(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "ended"

        ns2, is_new, _ = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert ns2.session_id == ""

    def test_provider_switch_resets_session(self, orch_ia: Orchestrator) -> None:
        # Start with a codex model so the session is created for provider "codex"
        orch_ia._config.model = "gpt-5.3-codex"
        ns1, _, notice1 = _get_or_create_interagent_session(orch_ia, "main")
        assert notice1 == ""
        assert ns1.provider == "codex"
        ns1.status = "idle"
        ns1.session_id = "codex-sess-1"

        # Switch to a claude model → different provider
        orch_ia._config.model = "sonnet"

        ns2, is_new, notice2 = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is True
        assert ns2.session_id == ""  # Fresh — old codex session discarded
        assert "provider" in notice2.lower()
        assert ns2.provider == "claude"

    def test_same_provider_no_notice(self, orch_ia: Orchestrator) -> None:
        ns1, _, _ = _get_or_create_interagent_session(orch_ia, "main")
        ns1.status = "idle"

        _ns2, is_new, notice = _get_or_create_interagent_session(orch_ia, "main")
        assert is_new is False
        assert notice == ""


class TestHandleInteragentMessage:
    """Test handle_interagent_message."""

    async def test_returns_result_and_session_name(self, orch_ia: Orchestrator) -> None:
        outcome = await orch_ia.handle_interagent_message("main", "Do something")
        assert outcome.text == "done"
        assert outcome.session_name == "ia-main"
        assert outcome.notice == ""

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
        outcome = await orch_ia.handle_interagent_message("main", "Task 2")
        assert outcome.text == "continued"

        # Verify resume_session was passed
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.resume_session == "sess-001"

    async def test_new_session_flag_starts_fresh(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_interagent_message("main", "Task 1")

        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="sess-new", result="fresh start")
        )
        outcome = await orch_ia.handle_interagent_message("main", "New task", new_session=True)
        assert outcome.text == "fresh start"

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
        assert request.process_label.startswith("ns:ia-main:")
        assert len(request.process_label.rsplit(":", 1)[1]) == 16

    async def test_error_returns_error_text(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))
        outcome = await orch_ia.handle_interagent_message("main", "Crash")
        assert "Error" in outcome.text
        assert outcome.session_name == "ia-main"

    async def test_provider_switch_returns_notice(self, orch_ia: Orchestrator) -> None:
        # Start with codex provider
        orch_ia._config.model = "gpt-5.3-codex"
        await orch_ia.handle_interagent_message("main", "Task 1")

        # Switch to claude provider
        orch_ia._config.model = "sonnet"
        orch_ia._cli_service.execute = AsyncMock(
            return_value=CLIResponse(session_id="claude-sess", result="switched")
        )
        outcome = await orch_ia.handle_interagent_message("main", "Task 2")
        assert outcome.text == "switched"
        assert "provider" in outcome.notice.lower()
        # Fresh session → no resume
        call_args = orch_ia._cli_service.execute.call_args
        assert call_args[0][0].resume_session is None

    async def test_session_idle_after_error(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("crash"))
        await orch_ia.handle_interagent_message("main", "Crash")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        assert ns.status == "idle"

    async def test_handle_interagent_message_recovers_from_stale_session(
        self, orch_ia: Orchestrator
    ) -> None:
        """#81: when the first cli.execute returns a stale-session error, the
        handler must end the named session, create a fresh one, retry without
        ``resume_session``, and prepend a recovery notice to
        ``provider_switch_notice`` so the caller sees what happened."""
        # Seed an idle session with a (about to be) stale session_id.
        await orch_ia.handle_interagent_message("main", "Task 1")
        ns = orch_ia._named_sessions.get(12345, "ia-main")
        assert ns is not None
        ns.session_id = "stale-id"
        ns.status = "idle"

        stale_response = CLIResponse(
            result="No conversation found with session ID: stale-id",
            is_error=True,
        )
        fresh_response = CLIResponse(session_id="fresh-sess", result="fresh response")
        orch_ia._cli_service.execute = AsyncMock(side_effect=[stale_response, fresh_response])

        outcome = await orch_ia.handle_interagent_message("main", "Task 2")

        assert outcome.text == "fresh response"
        assert outcome.session_name == "ia-main"
        assert "stale" in outcome.notice.lower()
        assert orch_ia._cli_service.execute.call_count == 2
        first_call_req = orch_ia._cli_service.execute.call_args_list[0][0][0]
        second_call_req = orch_ia._cli_service.execute.call_args_list[1][0][0]
        assert first_call_req.resume_session == "stale-id"
        assert second_call_req.resume_session is None


class TestHandleAsyncInteragentResult:
    """Test handle_async_interagent_result."""

    def _make_result(
        self,
        result_text: str = "Result",
        *,
        recipient: str = "helper",
        task_id: str = "task-001",
        session_name: str = "",
        original_message: str = "",
    ) -> AsyncInterAgentResult:
        return AsyncInterAgentResult(
            task_id=task_id,
            sender="codex",
            recipient=recipient,
            message_preview=result_text[:60],
            result_text=result_text,
            session_name=session_name,
            original_message=original_message,
        )

    async def test_basic_result_processing(self, orch_ia: Orchestrator) -> None:
        result = await orch_ia.handle_async_interagent_result(
            self._make_result("Task completed successfully"),
            chat_id=12345,
        )
        assert result == "done"

    async def test_prompt_contains_session_hint(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_async_interagent_result(
            self._make_result(session_name="ia-codex"),
            chat_id=12345,
        )
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert "ia-codex" in request.prompt
        assert "@ia-codex" in request.prompt

    async def test_prompt_without_session_name(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_async_interagent_result(
            self._make_result(session_name=""),
            chat_id=12345,
        )
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert "@" not in request.prompt or "ia-" not in request.prompt

    async def test_error_handling(self, orch_ia: Orchestrator) -> None:
        orch_ia._cli_service.execute = AsyncMock(side_effect=RuntimeError("oops"))
        result = await orch_ia.handle_async_interagent_result(
            self._make_result(),
        )
        assert "Error" in result

    async def test_prompt_contains_original_message(self, orch_ia: Orchestrator) -> None:
        await orch_ia.handle_async_interagent_result(
            self._make_result(original_message="Check the system specs"),
            chat_id=12345,
        )
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert "Check the system specs" in request.prompt
        assert "Original task you sent" in request.prompt

    async def test_resumes_current_active_session(self, orch_ia: Orchestrator) -> None:
        from ductor_bot.cli.types import AgentResponse
        from ductor_bot.session import SessionData

        sd = SessionData(12345, session_id="active-session-999")
        orch_ia._sessions.get_active = AsyncMock(return_value=sd)
        orch_ia._sessions.update_session = AsyncMock()
        orch_ia._cli_service.execute = AsyncMock(
            return_value=AgentResponse(result="done", session_id="active-session-999"),
        )

        await orch_ia.handle_async_interagent_result(
            self._make_result(),
            chat_id=12345,
        )
        call_args = orch_ia._cli_service.execute.call_args
        request = call_args[0][0]
        assert request.resume_session == "active-session-999"
