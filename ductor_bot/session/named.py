"""Named background sessions with follow-up support and JSON persistence."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ductor_bot.infra.json_store import atomic_json_save, load_json

logger = logging.getLogger(__name__)

_ADJECTIVES: tuple[str, ...] = (
    "bold",
    "blue",
    "calm",
    "cool",
    "dark",
    "deep",
    "fast",
    "firm",
    "glad",
    "gold",
    "keen",
    "kind",
    "late",
    "lean",
    "live",
    "long",
    "loud",
    "mint",
    "neat",
    "next",
    "pale",
    "pure",
    "rare",
    "real",
    "red",
    "rich",
    "safe",
    "slim",
    "soft",
    "tall",
    "tidy",
    "tiny",
    "true",
    "vast",
    "warm",
    "wild",
    "wise",
    "wry",
    "zen",
    "zinc",
)

_NOUNS: tuple[str, ...] = (
    "ant",
    "ape",
    "bat",
    "bay",
    "bee",
    "cat",
    "cod",
    "cow",
    "cub",
    "doe",
    "eel",
    "elk",
    "elm",
    "emu",
    "fin",
    "fly",
    "fox",
    "gem",
    "gnu",
    "gull",
    "hare",
    "hawk",
    "ibis",
    "jay",
    "koi",
    "lark",
    "lynx",
    "mole",
    "moth",
    "newt",
    "oak",
    "orb",
    "orca",
    "owl",
    "paw",
    "pike",
    "puma",
    "ray",
    "seal",
    "star",
    "swan",
    "toad",
    "vole",
    "wasp",
    "wolf",
    "wren",
    "yak",
)

MAX_SESSIONS_PER_CHAT = 10

_MAX_NAME_ATTEMPTS = 50
IA_IDLE_PER_SENDER = 8
IA_IDLE_GLOBAL = 32
IA_RUNNING_CEILING = 16
IA_NAME_MAX_BYTES = 40


def generate_name(existing: set[str]) -> str:
    """Generate a unique compact name (e.g. 'redowl') not in *existing*."""
    for _ in range(_MAX_NAME_ATTEMPTS):
        name = f"{secrets.choice(_ADJECTIVES)}{secrets.choice(_NOUNS)}"
        if name not in existing:
            return name
    # Fallback: append digit
    base = f"{secrets.choice(_ADJECTIVES)}{secrets.choice(_NOUNS)}"
    for i in range(2, 100):
        candidate = f"{base}{i}"
        if candidate not in existing:
            return candidate
    msg = "Could not generate unique session name"
    raise RuntimeError(msg)


@dataclass(slots=True)
class NamedSession:
    """State for a named background session."""

    name: str
    chat_id: int
    provider: str
    model: str
    session_id: str
    prompt_preview: str
    status: str  # "running" | "idle" | "ended"
    created_at: float
    message_count: int = 0
    last_prompt: str = ""
    transport: str = "tg"
    ia_sender: str | None = None
    ia_transport: str | None = None
    ia_chat_id: int | None = None
    ia_topic_id: int | None = None
    reservation_gen: int = 0
    last_active_at: float | None = None
    last_topic_id: int | None = None


def is_interagent_session(ns: NamedSession) -> bool:
    """Return True for legacy or scoped inter-agent named sessions."""
    return ns.ia_sender is not None or ns.name.startswith(("ia-", "ia."))


def named_process_label(name: str, token: str) -> str:
    """Return the execution-scoped process label for a named session."""
    return f"ns:{name}:{token}"


def _session_from_dict(data: dict[str, Any]) -> NamedSession:
    """Reconstruct a NamedSession from a JSON-serialized dict."""
    return NamedSession(
        name=str(data.get("name", "")),
        chat_id=int(data.get("chat_id", 0)),
        provider=str(data.get("provider", "")),
        model=str(data.get("model", "")),
        session_id=str(data.get("session_id", "")),
        prompt_preview=str(data.get("prompt_preview", "")),
        status=str(data.get("status", "ended")),
        created_at=float(data.get("created_at", 0.0)),
        message_count=int(data.get("message_count", 0)),
        last_prompt=str(data.get("last_prompt", data.get("prompt_preview", ""))),
        transport=str(data.get("transport", "tg")),
        ia_sender=data.get("ia_sender"),
        ia_transport=data.get("ia_transport"),
        ia_chat_id=int(data["ia_chat_id"]) if data.get("ia_chat_id") is not None else None,
        ia_topic_id=int(data["ia_topic_id"]) if data.get("ia_topic_id") is not None else None,
        reservation_gen=int(data.get("reservation_gen", 0)),
        last_active_at=float(data["last_active_at"]) if data.get("last_active_at") else None,
        last_topic_id=(
            int(data["last_topic_id"]) if data.get("last_topic_id") is not None else None
        ),
    )


class NamedSessionRegistry:
    """Registry of named sessions with JSON persistence.

    Sessions survive bot restarts. On startup, sessions with status
    ``"running"`` are downgraded to ``"idle"`` (the CLI process is gone
    but the CLI session ID may still be valid for ``--resume``).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._sessions: dict[tuple[int, str], NamedSession] = {}
        self._recovered_running: dict[tuple[int, str], NamedSession] = {}
        self._active_tokens: dict[tuple[int, str], str] = {}
        self._load()

    def begin_execution(self, chat_id: int, name: str) -> str:
        """Record and return a unique token for one named-session execution."""
        token = secrets.token_hex(8)
        self._active_tokens[(chat_id, name)] = token
        return token

    def active_execution_token(self, chat_id: int, name: str) -> str | None:
        """Return the active execution token, if any."""
        return self._active_tokens.get((chat_id, name))

    def finish_execution(self, chat_id: int, name: str, token: str) -> None:
        """Clear *token* only when it still owns the named execution."""
        key = (chat_id, name)
        if self._active_tokens.get(key) == token:
            self._active_tokens.pop(key, None)

    def _load(self) -> None:
        """Load sessions from JSON on disk."""
        raw = load_json(self._path)
        if not raw:
            return
        entries: list[dict[str, Any]] = raw.get("sessions", [])
        for entry in entries:
            ns = _session_from_dict(entry)
            if ns.status == "ended" or not ns.name:
                continue
            # Downgrade stale "running" to "idle" after restart
            if ns.status == "running":
                self._recovered_running[(ns.chat_id, ns.name)] = NamedSession(
                    name=ns.name,
                    chat_id=ns.chat_id,
                    provider=ns.provider,
                    model=ns.model,
                    session_id=ns.session_id,
                    prompt_preview=ns.prompt_preview,
                    status="idle",
                    created_at=ns.created_at,
                    message_count=ns.message_count,
                    last_prompt=ns.last_prompt,
                    transport=ns.transport,
                    ia_sender=ns.ia_sender,
                    ia_transport=ns.ia_transport,
                    ia_chat_id=ns.ia_chat_id,
                    ia_topic_id=ns.ia_topic_id,
                    reservation_gen=ns.reservation_gen,
                    last_active_at=ns.last_active_at,
                    last_topic_id=ns.last_topic_id,
                )
                ns.status = "idle"
            self._sessions[(ns.chat_id, ns.name)] = ns
        logger.info("Loaded %d named sessions from %s", len(self._sessions), self._path)

    def _persist(self) -> None:
        """Write all non-ended sessions to JSON."""
        entries = [asdict(ns) for ns in self._sessions.values() if ns.status != "ended"]
        atomic_json_save(self._path, {"sessions": entries})

    def create(  # noqa: PLR0913
        self,
        chat_id: int,
        provider: str,
        model: str,
        prompt_preview: str,
        *,
        transport: str = "tg",
        topic_id: int | None = None,
    ) -> NamedSession:
        """Create a new named session. Raises ValueError if limit exceeded."""
        active = {s.name for s in self.list_active(chat_id) if not is_interagent_session(s)}
        if len(active) >= MAX_SESSIONS_PER_CHAT:
            msg = f"Too many sessions ({MAX_SESSIONS_PER_CHAT} max)"
            raise ValueError(msg)

        name = generate_name(active)
        session = NamedSession(
            name=name,
            chat_id=chat_id,
            provider=provider,
            model=model,
            session_id="",
            prompt_preview=prompt_preview[:60],
            status="running",
            created_at=time.time(),
            transport=transport,
            reservation_gen=1,
            last_active_at=time.time(),
            last_topic_id=topic_id,
        )
        self._sessions[(chat_id, name)] = session
        self._persist()
        logger.info(
            "Named session created name=%s chat=%d provider=%s",
            name,
            chat_id,
            provider,
        )
        return session

    def get(self, chat_id: int, name: str) -> NamedSession | None:
        """Look up a named session (any status)."""
        return self._sessions.get((chat_id, name))

    def list_active(self, chat_id: int) -> list[NamedSession]:
        """Return all non-ended sessions for *chat_id*, ordered by creation."""
        return sorted(
            (s for s in self._sessions.values() if s.chat_id == chat_id and s.status != "ended"),
            key=lambda s: s.created_at,
        )

    def end_session(self, chat_id: int, name: str) -> bool:
        """Mark a session as ended. Returns True if found and ended."""
        ns = self._sessions.get((chat_id, name))
        if ns is None or ns.status == "ended":
            return False
        ns.status = "ended"
        ns.last_active_at = time.time()
        self._persist()
        logger.info("Named session ended name=%s chat=%d", name, chat_id)
        return True

    def end_all(self, chat_id: int) -> int:
        """End all active sessions for *chat_id*. Returns count ended."""
        count = 0
        for ns in self._sessions.values():
            if ns.chat_id == chat_id and ns.status != "ended":
                ns.status = "ended"
                ns.last_active_at = time.time()
                count += 1
        if count:
            self._persist()
            logger.info("All named sessions ended chat=%d count=%d", chat_id, count)
        return count

    def update_after_response(  # noqa: PLR0913
        self,
        chat_id: int,
        name: str,
        session_id: str,
        *,
        status: str = "idle",
        expected_session: NamedSession | None = None,
        reservation_gen: int | None = None,
    ) -> bool:
        """Update session state after a CLI response."""
        ns = self._sessions.get((chat_id, name))
        if ns is None:
            return False
        if expected_session is not None and ns is not expected_session:
            return False
        if ns.status == "ended":
            logger.warning("Ignoring update for ended named session name=%s chat=%d", name, chat_id)
            return False
        if reservation_gen is not None and ns.reservation_gen != reservation_gen:
            return False
        if ns.status != "running":
            return False
        if session_id:
            ns.session_id = session_id
        ns.message_count += 1
        ns.status = status
        ns.last_active_at = time.time()
        self._persist()
        return True

    def add(self, session: NamedSession) -> None:
        """Add a pre-built session to the registry and persist.

        Use this when the caller needs full control over the session name
        and fields (e.g. inter-agent sessions with deterministic names).
        """
        self._sessions[(session.chat_id, session.name)] = session
        self._persist()

    def mark_running(self, chat_id: int, name: str, prompt: str) -> None:
        """Mark a session as running and store the prompt for recovery."""
        ns = self._sessions.get((chat_id, name))
        if ns is None:
            return
        if ns.status == "ended":
            logger.warning("Ignoring running mark for ended named session name=%s chat=%d", name, chat_id)
            return
        ns.status = "running"
        ns.last_prompt = prompt[:4000]
        ns.last_active_at = time.time()
        self._persist()

    def reserve_followup(
        self,
        chat_id: int,
        name: str,
        prompt: str,
        *,
        topic_id: int | None = None,
        transport: str | None = None,
    ) -> int | None:
        """Reserve an idle named session for a background follow-up."""
        ns = self._sessions.get((chat_id, name))
        if ns is None or ns.status != "idle":
            return None
        ns.reservation_gen += 1
        ns.status = "running"
        ns.last_prompt = prompt[:4000]
        ns.last_topic_id = topic_id
        if transport:
            ns.transport = transport
        ns.last_active_at = time.time()
        self._persist()
        return ns.reservation_gen

    def rollback_reservation(self, chat_id: int, name: str, reservation_gen: int) -> bool:
        """Return a still-current reservation to idle without reviving ended sessions."""
        ns = self._sessions.get((chat_id, name))
        if ns is None or ns.status != "running" or ns.reservation_gen != reservation_gen:
            return False
        ns.status = "idle"
        ns.last_active_at = time.time()
        self._persist()
        return True

    def update_model(self, chat_id: int, name: str, model: str) -> bool:
        ns = self._sessions.get((chat_id, name))
        if ns is None or ns.status == "ended":
            return False
        if ns.model == model:
            return True
        ns.model = model
        self._persist()
        return True

    def find_interagent(
        self,
        chat_id: int,
        sender: str,
        transport: str,
        o_chat: int,
        o_topic: int | None,
    ) -> NamedSession | None:
        for ns in self._sessions.values():
            if ns.chat_id != chat_id or ns.status == "ended":
                continue
            if (
                ns.ia_sender == sender
                and ns.ia_transport == transport
                and ns.ia_chat_id == o_chat
                and ns.ia_topic_id == o_topic
            ):
                return ns
        return None

    def prune_interagent(self, chat_id: int, lock_pool: Any | None = None) -> list[str]:
        """End oldest idle inter-agent sessions above per-sender/global caps."""
        sessions = [
            s
            for s in self._sessions.values()
            if s.chat_id == chat_id and s.status == "idle" and is_interagent_session(s)
        ]
        ended: list[str] = []

        def idle_key(ns: NamedSession) -> float:
            return ns.last_active_at if ns.last_active_at is not None else ns.created_at

        candidates: list[NamedSession] = []
        by_sender: dict[str, list[NamedSession]] = {}
        for ns in sessions:
            by_sender.setdefault(ns.ia_sender or ns.name, []).append(ns)
        for group in by_sender.values():
            group.sort(key=idle_key)
            candidates.extend(group[:-IA_IDLE_PER_SENDER])
        candidate_keys = {(ns.chat_id, ns.name) for ns in candidates}
        survivors = sorted(
            (ns for ns in sessions if (ns.chat_id, ns.name) not in candidate_keys),
            key=idle_key,
        )
        if len(survivors) > IA_IDLE_GLOBAL:
            candidates.extend(survivors[: len(survivors) - IA_IDLE_GLOBAL])
        seen: set[tuple[int, str]] = set()
        for ns in candidates:
            key = (ns.chat_id, ns.name)
            if key in seen or ns.status != "idle":
                continue
            seen.add(key)
            if lock_pool is not None and (lock_pool.is_locked(key) or lock_pool.has_waiters(key)):
                continue
            ns.status = "ended"
            ns.last_active_at = time.time()
            ended.append(ns.name)
            if lock_pool is not None:
                lock_pool.evict_if_unused(key)
            self._sessions.pop(key, None)
        if ended:
            self._persist()
        return ended

    def pop_recovered_running(self, chat_id: int | None = None) -> list[NamedSession]:
        """Return sessions that were running at last shutdown, then clear them.

        If *chat_id* is given, only return sessions for that chat.
        Excludes inter-agent sessions (``ia-`` prefix).
        """
        results: list[NamedSession] = []
        to_remove: list[tuple[int, str]] = []
        for key, ns in self._recovered_running.items():
            if chat_id is not None and ns.chat_id != chat_id:
                continue
            if is_interagent_session(ns):
                continue
            results.append(ns)
            to_remove.append(key)
        for key in to_remove:
            del self._recovered_running[key]
        return sorted(results, key=lambda s: s.created_at)

    def active_names(self, chat_id: int) -> set[str]:
        """Return the set of active session names for collision checks."""
        return {
            s.name for s in self._sessions.values() if s.chat_id == chat_id and s.status != "ended"
        }
