"""Tests for the heartbeat flow (orchestrator integration)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import time_machine

from ductor_bot.cli.types import AgentResponse
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import heartbeat_flow
from ductor_bot.session.key import SessionKey


def _mock_response(**kwargs: Any) -> AgentResponse:
    defaults: dict[str, Any] = {
        "result": "HEARTBEAT_OK",
        "session_id": "sess-123",
        "is_error": False,
        "cost_usd": 0.01,
        "total_tokens": 100,
    }
    defaults.update(kwargs)
    return AgentResponse(**defaults)


def _past_cooldown() -> time_machine.travel:
    """Return a time_machine context that jumps 10 minutes into the future."""
    return time_machine.travel(datetime.now(UTC) + timedelta(minutes=10))


@pytest.fixture
def orch(orch: Orchestrator) -> Orchestrator:
    return orch


async def test_heartbeat_ok_returns_none(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEARTBEAT_OK response is suppressed."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service,
            "execute",
            AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK")),
        )
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result is None


async def test_heartbeat_alert_returns_text(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-OK response returns the alert text."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    alert = "Hey! I found something interesting about Python 3.14!"
    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result=alert))
        )
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result == alert


async def test_heartbeat_skips_new_session(orch: Orchestrator) -> None:
    """Heartbeat does nothing if there is no established session."""
    result = await heartbeat_flow(orch, SessionKey(chat_id=999))
    assert result is None


async def test_heartbeat_ok_does_not_increment_message_count(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEARTBEAT_OK should not change message_count."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    session_before = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_before is not None
    count_before = session_before.message_count

    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service,
            "execute",
            AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK")),
        )
        await heartbeat_flow(orch, SessionKey(chat_id=1))

    session_after = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_after is not None
    assert session_after.message_count == count_before


async def test_heartbeat_alert_increments_message_count(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alert responses should update session state normally."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    session_before = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_before is not None
    count_before = session_before.message_count

    alert = "Check out this cool fact!"
    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result=alert))
        )
        await heartbeat_flow(orch, SessionKey(chat_id=1))

    session_after = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_after is not None
    assert session_after.message_count == count_before + 1


async def test_heartbeat_cli_error_returns_none(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI errors during heartbeat are silently logged, not propagated."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service,
            "execute",
            AsyncMock(return_value=_mock_response(is_error=True, result="Rate limited")),
        )
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result is None


async def test_heartbeat_does_not_apply_hooks(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat prompt should not have message hooks injected."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )

    # Send 5 messages to reach hook threshold
    for _ in range(5):
        await normal(orch, SessionKey(chat_id=1), "msg")

    # Now heartbeat -- the prompt should be the raw heartbeat prompt, no REMINDER
    with _past_cooldown():
        hb_mock = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
        monkeypatch.setattr(orch._cli_service, "execute", hb_mock)
        await heartbeat_flow(orch, SessionKey(chat_id=1))

        hb_request = hb_mock.call_args[0][0]
        assert "REMINDER" not in hb_request.prompt
        assert "heartbeat" in hb_request.prompt.lower()


async def test_heartbeat_skips_during_cooldown(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat is skipped if user was active within cooldown_minutes."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "just chatting")

    # Immediately after user message -> within cooldown -> skip
    cooldown_mock = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
    monkeypatch.setattr(orch._cli_service, "execute", cooldown_mock)
    result = await heartbeat_flow(orch, SessionKey(chat_id=1))
    assert result is None
    # CLI should NOT have been called since cooldown skipped early
    cooldown_mock.assert_not_awaited()


async def test_heartbeat_runs_after_cooldown(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat fires when last_active is older than cooldown_minutes."""
    from ductor_bot.orchestrator.flows import normal

    monkeypatch.setattr(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(result="Hello"))
    )
    await normal(orch, SessionKey(chat_id=1), "init")

    # Travel 10 minutes into the future -> past default 5 min cooldown
    future = datetime.now(UTC) + timedelta(minutes=10)
    with time_machine.travel(future):
        after_cooldown_mock = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
        monkeypatch.setattr(orch._cli_service, "execute", after_cooldown_mock)
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result is None
        # CLI WAS called (cooldown passed, returned OK)
        after_cooldown_mock.assert_awaited_once()


async def test_heartbeat_skips_when_session_provider_differs_from_configured_provider(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeat should skip when active session provider does not match current provider target."""
    orch._providers._available_providers = frozenset({"codex"})

    key = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(key, provider="codex", model="opus")
    session.session_id = "legacy-heartbeat-sid"
    await orch._sessions.update_session(session)
    count_before = session.message_count

    with _past_cooldown():
        monkeypatch.setattr(
            orch._cli_service,
            "execute",
            AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK")),
        )
        result = await heartbeat_flow(orch, SessionKey(chat_id=1))
        assert result is None

    session_after = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session_after is not None
    assert session_after.provider == "codex"
    assert session_after.model == "opus"
    assert session_after.message_count == count_before


async def test_heartbeat_preserves_session_model(
    orch: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heartbeat must not rewrite the session's model to the configured default.

    The session's model is a per-chat user choice (``/model``). The heartbeat is
    a read-only liveness check, so it has to leave that choice alone -- and run
    the turn with it, the way it already does for ``reasoning_effort``.
    """
    key = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(key, provider="claude", model="sonnet")
    session.session_id = "sid-model-preserved"
    await orch._sessions.update_session(session)
    assert orch._config.model != "sonnet"

    with _past_cooldown():
        execute = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
        monkeypatch.setattr(orch._cli_service, "execute", execute)
        assert await heartbeat_flow(orch, key) is None

    # The turn ran with the session's model, not the configured default.
    assert execute.await_args.args[0].model_override == "sonnet"
    # And the stored session still carries the user's choice.
    stored = await orch._sessions.get_active(key)
    assert stored is not None
    assert stored.model == "sonnet"
