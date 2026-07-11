"""Session injection: routes inter-agent messages and task questions through CLIService.

Extracts the common "build prompt → get active session → execute → update"
pattern from the Orchestrator into reusable helpers.

Note: task *results* are injected via the MessageBus (see ``bus.adapters``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from typing import TYPE_CHECKING

from ductor_bot.cli.types import AgentRequest
from ductor_bot.interagent_types import InterAgentOrigin, InterAgentOutcome
from ductor_bot.orchestrator.flows import _is_invalid_session, _update_session
from ductor_bot.session.key import SessionKey
from ductor_bot.session.named import IA_NAME_MAX_BYTES, NamedSession, named_process_label

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

_TRANSPORT_ALIASES = {"telegram": "tg", "matrix": "mx"}


def _transport_id(value: str) -> str:
    """Return the short transport id used by SessionKey and Envelope."""
    stripped = value.strip().lower()
    return _TRANSPORT_ALIASES.get(stripped, stripped or "tg")


# ---------------------------------------------------------------------------
# Shared injection helper
# ---------------------------------------------------------------------------


async def _inject_prompt(  # noqa: PLR0913
    orch: Orchestrator,
    prompt: str,
    chat_id: int,
    process_label: str,
    *,
    topic_id: int | None = None,
    transport: str = "tg",
) -> str:
    """Execute *prompt* in the current active session and update session state.

    Shared by ``handle_async_interagent_result`` and ``inject_prompt``.
    """
    key = SessionKey(transport=transport, chat_id=chat_id, topic_id=topic_id)
    active = await orch._sessions.get_active(key)
    resume_id = active.session_id if active else None

    request = AgentRequest(
        prompt=prompt,
        chat_id=chat_id,
        topic_id=topic_id,
        transport=transport,
        process_label=process_label,
        provider_override=active.provider if active else None,
        model_override=active.model if active else None,
        resume_session=resume_id,
        timeout_seconds=orch._config.cli_timeout,
    )

    response = await orch._cli_service.execute(request)

    if active and response:
        await _update_session(orch, active, response)

    return response.result if response else ""


# ---------------------------------------------------------------------------
# Inter-agent session helpers
# ---------------------------------------------------------------------------


def _interagent_chat_id(orch: Orchestrator) -> int:
    """Return the anchor chat_id for recipient inter-agent sessions."""
    if not orch._config.allowed_user_ids:
        logger.warning("No allowed_user_ids configured; inter-agent sessions use chat_id=0")
        return 0
    return orch._config.allowed_user_ids[0]


def _normalise_origin(origin: InterAgentOrigin | None) -> InterAgentOrigin | None:
    if origin is None:
        return None
    transport = _transport_id(origin.transport)
    normalized = InterAgentOrigin(
        transport=transport,
        chat_id=origin.chat_id,
        topic_id=origin.topic_id,
    )
    return normalized if normalized.valid() else None


def _sender_slug(sender: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "", sender.lower())[:16]
    return slug or "agent"


def _derive_ia_name(
    sender: str,
    origin: InterAgentOrigin | None,
    *,
    hash_len: int = 6,
) -> str:
    """Derive a legacy or scoped IA display name for a new session."""
    if origin is None or not origin.valid():
        return f"ia-{sender}"
    slug = _sender_slug(sender)
    digest = hashlib.sha1(
        f"{origin.transport}:{origin.chat_id}:{origin.topic_id}:{sender}".encode(),
        usedforsecurity=False,
    ).hexdigest()[:hash_len]
    topic_token = f".t{origin.topic_id}" if origin.topic_id is not None else ""
    name = f"ia.{slug}{topic_token}.x{digest}"
    if len(name.encode("utf-8")) <= IA_NAME_MAX_BYTES:
        return name
    name = f"ia.{slug}.x{digest}"
    if len(name.encode("utf-8")) <= IA_NAME_MAX_BYTES:
        return name
    budget = IA_NAME_MAX_BYTES - len(f"ia..x{digest}".encode())
    return f"ia.{slug[: max(1, budget)]}.x{digest}"


def _same_identity(ns: NamedSession, sender: str, origin: InterAgentOrigin | None) -> bool:
    if origin is None:
        return ns.name == f"ia-{sender}" and ns.ia_sender is None
    return (
        ns.ia_sender == sender
        and ns.ia_transport == origin.transport
        and ns.ia_chat_id == origin.chat_id
        and ns.ia_topic_id == origin.topic_id
    )


def _lookup_interagent(
    orch: Orchestrator,
    chat_id: int,
    sender: str,
    origin: InterAgentOrigin | None,
) -> NamedSession | None:
    if origin is None:
        ns = orch._named_sessions.get(chat_id, f"ia-{sender}")
        return ns if ns is not None and ns.status != "ended" else None
    return orch._named_sessions.find_interagent(
        chat_id,
        sender,
        origin.transport,
        origin.chat_id,
        origin.topic_id,
    )


def _get_or_create_interagent_session(
    orch: Orchestrator,
    sender: str,
    *,
    new_session: bool = False,
    origin: InterAgentOrigin | None = None,
    candidate_name: str | None = None,
) -> tuple[NamedSession, bool, str]:
    """Get or create an inter-agent named session using field identity first."""
    chat_id = _interagent_chat_id(orch)
    origin = _normalise_origin(origin)
    provider_switch_notice = ""

    existing = _lookup_interagent(orch, chat_id, sender, origin)
    if new_session and existing is not None:
        orch._named_sessions.end_session(chat_id, existing.name)
        orch._ns_locks.evict_if_unused((chat_id, existing.name))
        existing = None

    model_name, provider_name = orch.resolve_runtime_target(orch._config.model)

    if existing is not None and existing.status != "ended":
        if existing.provider != provider_name:
            old_provider = existing.provider
            orch._named_sessions.end_session(chat_id, existing.name)
            orch._ns_locks.evict_if_unused((chat_id, existing.name))
            provider_switch_notice = (
                f"Agent `{orch._cli_service._config.agent_name}` switched "
                f"provider from `{old_provider}` to `{provider_name}`.\n"
                f"The previous inter-agent session `{existing.name}` is no longer "
                f"resumable and has been ended.\n"
                f"A new session was started with `{provider_name}`."
            )
        else:
            if existing.model != model_name:
                orch._named_sessions.update_model(chat_id, existing.name, model_name)
                existing.model = model_name
            return existing, False, ""

    session_name = candidate_name or _derive_ia_name(sender, origin)
    occupant = orch._named_sessions.get(chat_id, session_name)
    if (
        occupant is not None
        and occupant.status != "ended"
        and not _same_identity(occupant, sender, origin)
    ):
        msg = f"Inter-agent session name collision: {session_name}"
        raise RuntimeError(msg)

    ns = NamedSession(
        name=session_name,
        chat_id=chat_id,
        provider=provider_name,
        model=model_name,
        session_id="",
        prompt_preview=f"Inter-agent session with {sender}",
        status="running",
        created_at=time.time(),
        transport=_transport_id(orch._config.transport),
        ia_sender=sender if origin is not None else None,
        ia_transport=origin.transport if origin is not None else None,
        ia_chat_id=origin.chat_id if origin is not None else None,
        ia_topic_id=origin.topic_id if origin is not None else None,
        reservation_gen=1,
        last_active_at=time.time(),
    )
    orch._named_sessions.add(ns)
    logger.info("Inter-agent named session created: %s (sender=%s)", session_name, sender)
    return ns, True, provider_switch_notice


async def handle_interagent_message(  # noqa: C901, PLR0911, PLR0912, PLR0915
    orch: Orchestrator,
    sender: str,
    message: str,
    *,
    new_session: bool = False,
    origin: InterAgentOrigin | None = None,
) -> InterAgentOutcome:
    """Process a message from another agent via the InterAgentBus."""
    own_name = orch._cli_service._config.agent_name
    chat_id = _interagent_chat_id(orch)
    transport = _transport_id(orch._config.transport)
    norm_origin = _normalise_origin(origin)
    hash_len = 6

    while True:
        existing_identity = _lookup_interagent(orch, chat_id, sender, norm_origin)
        candidate_name = (
            existing_identity.name
            if existing_identity is not None and existing_identity.status != "ended"
            else _derive_ia_name(sender, norm_origin, hash_len=hash_len)
        )
        key = (chat_id, candidate_name)
        if new_session:
            held = orch._ns_locks.try_acquire_nowait(key)
            if held is None:
                occupant = orch._named_sessions.get(chat_id, candidate_name)
                if (
                    occupant is not None
                    and occupant.status == "running"
                    and _same_identity(occupant, sender, norm_origin)
                ):
                    return InterAgentOutcome(
                        "session busy - retry after completion",
                        candidate_name,
                        "",
                        ok=False,
                        error_kind="busy",
                    )
                if (
                    occupant is not None
                    and occupant.status != "ended"
                    and not _same_identity(occupant, sender, norm_origin)
                ):
                    hash_len += 2
                    continue
                await asyncio.sleep(0.05)
                held = orch._ns_locks.try_acquire_nowait(key)
                if held is None:
                    return InterAgentOutcome(
                        "session busy - retry after completion",
                        candidate_name,
                        "",
                        ok=False,
                        error_kind="busy",
                    )
            cm = held
        else:
            cm = orch._ns_locks.acquire(key)

        async with cm:
            occupant = orch._named_sessions.get(chat_id, candidate_name)
            if (
                occupant is not None
                and occupant.status == "running"
                and _same_identity(occupant, sender, norm_origin)
            ):
                return InterAgentOutcome(
                    "session busy - retry after completion",
                    occupant.name,
                    "",
                    ok=False,
                    error_kind="busy",
                )
            if (
                occupant is not None
                and occupant.status != "ended"
                and not _same_identity(occupant, sender, norm_origin)
            ):
                hash_len += 2
                continue
            ns, is_new, provider_switch_notice = _get_or_create_interagent_session(
                orch,
                sender,
                new_session=new_session,
                origin=norm_origin,
                candidate_name=candidate_name,
            )
            if ns.name != candidate_name:
                candidate_name = ns.name
                continue
            if new_session and not is_new and ns.status == "running":
                return InterAgentOutcome(
                    "session busy - retry after completion",
                    ns.name,
                    provider_switch_notice,
                    ok=False,
                    error_kind="busy",
                )

            prompt = (
                f"[INTER-AGENT MESSAGE from '{sender}' to '{own_name}']\n"
                f"{message}\n"
                f"[END INTER-AGENT MESSAGE]\n\n"
                f"You are agent '{own_name}'. Respond to this inter-agent request "
                f"from '{sender}'. Be direct and concise."
            )

            if not orch._ia_limiter.try_acquire():
                if is_new:
                    orch._named_sessions.end_session(chat_id, ns.name)
                    orch._ns_locks.evict_if_unused((chat_id, ns.name))
                return InterAgentOutcome(
                    "inter-agent running ceiling reached",
                    ns.name,
                    provider_switch_notice,
                    ok=False,
                    error_kind="ceiling",
                )
            execution_token: str | None = None
            try:
                if is_new:
                    generation = ns.reservation_gen
                    ns.last_prompt = prompt[:4000]
                else:
                    reserved = orch._named_sessions.reserve_followup(
                        chat_id,
                        ns.name,
                        prompt,
                        topic_id=norm_origin.topic_id if norm_origin else None,
                        transport=transport,
                    )
                    if reserved is None:
                        return InterAgentOutcome(
                            "session busy - retry after completion",
                            ns.name,
                            provider_switch_notice,
                            ok=False,
                            error_kind="busy",
                        )
                    generation = reserved
                execution_token = orch._named_sessions.begin_execution(chat_id, ns.name)
                request = AgentRequest(
                    prompt=prompt,
                    chat_id=chat_id,
                    transport=transport,
                    process_label=named_process_label(ns.name, execution_token),
                    resume_session=ns.session_id or None,
                    timeout_seconds=orch._config.cli_timeout,
                )
                try:
                    response = await orch._cli_service.execute(request)
                except asyncio.CancelledError:
                    orch._named_sessions.update_after_response(
                        chat_id,
                        ns.name,
                        "",
                        expected_session=ns,
                        reservation_gen=generation,
                    )
                    raise
                except Exception:
                    orch._named_sessions.update_after_response(
                        chat_id,
                        ns.name,
                        "",
                        expected_session=ns,
                        reservation_gen=generation,
                    )
                    logger.exception("Inter-agent message handling failed (from=%s)", sender)
                    return InterAgentOutcome(
                        f"Error processing inter-agent message from '{sender}'",
                        ns.name,
                        provider_switch_notice,
                        ok=False,
                        error_kind="execution",
                    )

                if ns.status == "ended":
                    return InterAgentOutcome(
                        "Inter-agent session ended during execution",
                        ns.name,
                        provider_switch_notice,
                        ok=False,
                        error_kind="execution",
                    )

                if _is_invalid_session(response):
                    stale_id = ns.session_id
                    logger.warning(
                        "Inter-agent session stale (from=%s session=%s stale_id=%s) -- retrying",
                        sender,
                        ns.name,
                        stale_id,
                    )
                    orch._named_sessions.end_session(chat_id, ns.name)
                    orch._ns_locks.evict_if_unused((chat_id, ns.name))
                    ns, _, _ = _get_or_create_interagent_session(
                        orch,
                        sender,
                        new_session=True,
                        origin=norm_origin,
                        candidate_name=candidate_name,
                    )
                    generation = ns.reservation_gen
                    retry_request = AgentRequest(
                        prompt=prompt,
                        chat_id=chat_id,
                        transport=transport,
                        process_label=named_process_label(ns.name, execution_token),
                        resume_session=None,
                        timeout_seconds=orch._config.cli_timeout,
                    )
                    try:
                        response = await orch._cli_service.execute(retry_request)
                    except asyncio.CancelledError:
                        orch._named_sessions.update_after_response(
                            chat_id,
                            ns.name,
                            "",
                            expected_session=ns,
                            reservation_gen=generation,
                        )
                        raise
                    except Exception:
                        orch._named_sessions.update_after_response(
                            chat_id,
                            ns.name,
                            "",
                            expected_session=ns,
                            reservation_gen=generation,
                        )
                        logger.exception("Inter-agent retry failed (from=%s)", sender)
                        return InterAgentOutcome(
                            f"Error processing inter-agent message from '{sender}' "
                            "(after stale-session retry)",
                            ns.name,
                            provider_switch_notice,
                            ok=False,
                            error_kind="execution",
                        )
                    if ns.status == "ended":
                        return InterAgentOutcome(
                            "Inter-agent session ended during execution",
                            ns.name,
                            provider_switch_notice,
                            ok=False,
                            error_kind="execution",
                        )
                    recovery_notice = (
                        f"Inter-agent session `{ns.name}` was stale "
                        f"(CLI rejected session `{stale_id}`); started a fresh session "
                        f"and retried. This is normal after a CLI update."
                    )
                    provider_switch_notice = (
                        f"{provider_switch_notice}\n{recovery_notice}".strip()
                        if provider_switch_notice
                        else recovery_notice
                    )

                orch._named_sessions.update_after_response(
                    chat_id,
                    ns.name,
                    response.session_id if response else "",
                    expected_session=ns,
                    reservation_gen=generation,
                )
                if response is None:
                    return InterAgentOutcome(
                        "Inter-agent CLI execution returned no response",
                        ns.name,
                        provider_switch_notice,
                        ok=False,
                        error_kind="execution",
                    )
                if response.is_error:
                    return InterAgentOutcome(
                        response.result or "Inter-agent CLI execution failed",
                        ns.name,
                        provider_switch_notice,
                        ok=False,
                        error_kind="cli",
                    )
                return InterAgentOutcome(
                    response.result,
                    ns.name,
                    provider_switch_notice,
                    ok=True,
                )
            finally:
                if execution_token is not None:
                    orch._process_registry.clear_label_abort(
                        chat_id, named_process_label(ns.name, execution_token)
                    )
                    orch._named_sessions.finish_execution(
                        chat_id, ns.name, execution_token
                    )
                orch._ia_limiter.release()
                orch._named_sessions.prune_interagent(chat_id, orch._ns_locks)


async def handle_async_interagent_result(
    orch: Orchestrator,
    result: AsyncInterAgentResult,
    *,
    chat_id: int = 0,
) -> str:
    """Inject an async inter-agent result into the current active session.

    Called when another agent completes an async request we sent.
    Resumes the *current* active session (not the one that was active when
    the task was dispatched) so the agent has full conversation context.

    The prompt is self-contained: it includes both the original task
    description and the sub-agent's response, so the agent can process
    the result even if the session changed (``/new``, provider switch).

    Caller must hold the per-chat lock to prevent concurrent session access.
    """
    own_name = orch._cli_service._config.agent_name
    recipient = result.recipient
    task_id = result.task_id

    session_hint = (
        f"\nThe recipient processed this in session `{result.session_name}`. "
        f"The user can continue this session in the recipient's Telegram chat "
        f"via `@{result.session_name} <message>`."
        if result.session_name
        else ""
    )

    task_context = (
        f"\n\nOriginal task you sent to '{recipient}':\n{result.original_message}"
        if result.original_message
        else ""
    )

    prompt = (
        f"[ASYNC INTER-AGENT RESPONSE from '{recipient}' (task {task_id})]\n"
        f"{result.result_text}\n"
        f"[END ASYNC INTER-AGENT RESPONSE]{session_hint}{task_context}\n\n"
        f"You are agent '{own_name}'. Process this response from agent "
        f"'{recipient}' and communicate the relevant results to the user "
        f"in your Telegram chat."
    )

    logger.debug(
        "Injecting async result into main session: task=%s from=%s "
        "resume_session=%s original_msg_len=%d",
        task_id,
        recipient,
        "<pending>",
        len(result.original_message),
    )

    try:
        return await _inject_prompt(orch, prompt, chat_id, f"interagent-async:{recipient}")
    except Exception:
        logger.exception(
            "Async inter-agent result handling failed (from=%s)",
            recipient,
        )
        return f"Error processing async result from '{recipient}'"
