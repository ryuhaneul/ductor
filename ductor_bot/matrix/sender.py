"""Message sending for Matrix rooms.

Handles formatted messages, file uploads, and message splitting.
"""

from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.matrix.formatting import markdown_to_matrix_html

if TYPE_CHECKING:
    from nio import AsyncClient

logger = logging.getLogger(__name__)

_MAX_EVENT_SIZE = 60_000  # Matrix allows 65536 bytes per event; leave headroom
_FILE_TAG_RE = re.compile(r"<file:(.*?)>")


@dataclass
class MatrixSendOpts:
    """Options for sending a Matrix message."""

    reply_to_event_id: str | None = None
    thread_event_id: str | None = None
    allowed_roots: list[Path] | None = None


async def send_rich(
    client: AsyncClient,
    room_id: str,
    text: str,
    opts: MatrixSendOpts | None = None,
) -> str | None:
    """Send formatted message to Matrix room. Returns event_id of last sent message."""
    opts = opts or MatrixSendOpts()

    # 1. Extract file tags
    files: list[str] = _FILE_TAG_RE.findall(text)
    cleaned = _FILE_TAG_RE.sub("", text).strip()

    # 2. Convert markdown → (plain, html)
    plain, html_body = markdown_to_matrix_html(cleaned)

    # 3. Build message content
    last_event_id: str | None = None

    if cleaned:
        # Split if too large (rare for Matrix)
        chunks = _split_text(plain, html_body) if len(html_body.encode()) > _MAX_EVENT_SIZE else [(plain, html_body)]

        for p, h in chunks:
            content: dict[str, object] = {
                "msgtype": "m.text",
                "body": p,
                "format": "org.matrix.custom.html",
                "formatted_body": h,
            }

            # Thread support
            if opts.thread_event_id:
                content["m.relates_to"] = {
                    "rel_type": "m.thread",
                    "event_id": opts.thread_event_id,
                }
            elif opts.reply_to_event_id:
                content["m.relates_to"] = {
                    "m.in_reply_to": {"event_id": opts.reply_to_event_id},
                }

            resp = await client.room_send(room_id, "m.room.message", content)
            if hasattr(resp, "event_id"):
                last_event_id = resp.event_id
            else:
                logger.warning("room_send failed for %s: %s", room_id, resp)

    # 4. Upload and send files
    for file_path_str in files:
        file_path = Path(file_path_str)
        if not file_path.exists():
            logger.warning("File not found: %s", file_path)
            continue

        # Check allowed roots
        if opts.allowed_roots is not None:
            if not any(
                file_path.resolve().is_relative_to(root.resolve())
                for root in opts.allowed_roots
            ):
                logger.warning("File outside allowed roots: %s", file_path)
                continue

        event_id = await _upload_and_send_file(client, room_id, file_path)
        if event_id:
            last_event_id = event_id

    return last_event_id


async def _upload_and_send_file(
    client: AsyncClient,
    room_id: str,
    file_path: Path,
) -> str | None:
    """Upload a file to the homeserver and send as m.file/m.image."""
    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    file_data = file_path.read_bytes()
    file_name = file_path.name
    file_size = len(file_data)

    # Upload — nio expects a file-like object, not raw bytes
    resp, _keys = await client.upload(
        io.BytesIO(file_data),
        content_type=mime_type,
        filename=file_name,
        filesize=file_size,
    )

    if not hasattr(resp, "content_uri"):
        logger.warning("File upload failed for %s", file_path)
        return None

    # Determine message type
    if mime_type.startswith("image/"):
        msgtype = "m.image"
    elif mime_type.startswith("audio/"):
        msgtype = "m.audio"
    elif mime_type.startswith("video/"):
        msgtype = "m.video"
    else:
        msgtype = "m.file"

    content: dict[str, object] = {
        "msgtype": msgtype,
        "body": file_name,
        "url": resp.content_uri,
        "info": {
            "mimetype": mime_type,
            "size": file_size,
        },
    }

    send_resp = await client.room_send(room_id, "m.room.message", content)
    if hasattr(send_resp, "event_id"):
        return send_resp.event_id
    logger.warning("File send failed for %s in %s: %s", file_name, room_id, send_resp)
    return None


def _split_text(
    plain: str, html_body: str
) -> list[tuple[str, str]]:
    """Split text into chunks that fit within the Matrix event size limit.

    Splits the raw plain text by lines and converts each chunk independently
    so that plain/HTML line counts always match.
    """
    plain_lines = plain.split("\n")

    raw_chunks: list[str] = []
    cur_lines: list[str] = []
    cur_size = 0

    for line in plain_lines:
        line_size = len(line.encode()) + 1
        if cur_size + line_size > _MAX_EVENT_SIZE and cur_lines:
            raw_chunks.append("\n".join(cur_lines))
            cur_lines = []
            cur_size = 0
        cur_lines.append(line)
        cur_size += line_size

    if cur_lines:
        raw_chunks.append("\n".join(cur_lines))

    if not raw_chunks:
        return [("", "")]

    return [markdown_to_matrix_html(chunk) for chunk in raw_chunks]


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------


async def redact_message(
    client: AsyncClient,
    room_id: str,
    event_id: str,
) -> bool:
    """Redact (delete) a single message. Returns *True* on success."""
    try:
        resp = await client.room_redact(room_id, event_id)
        if hasattr(resp, "event_id"):
            return True
        logger.debug("Redact failed for %s in %s: %s", event_id, room_id, resp)
    except Exception:
        logger.debug("Redact error for %s in %s", event_id, room_id, exc_info=True)
    return False


async def redact_messages(
    client: AsyncClient,
    room_id: str,
    event_ids: list[str],
) -> int:
    """Redact multiple messages concurrently. Returns success count."""
    if not event_ids:
        return 0
    results = await asyncio.gather(
        *(redact_message(client, room_id, eid) for eid in event_ids),
        return_exceptions=True,
    )
    return sum(1 for r in results if r is True)
