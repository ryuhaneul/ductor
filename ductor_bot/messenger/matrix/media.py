"""Handle incoming Matrix media: download, index, and prompt injection."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.files.prompt import MediaInfo
from ductor_bot.files.prompt import build_media_prompt as _build_media_prompt_generic
from ductor_bot.files.storage import prepare_destination as _prepare_destination
from ductor_bot.files.storage import sanitize_filename as _sanitize_filename
from ductor_bot.files.storage import update_index as _update_index

if TYPE_CHECKING:
    from nio import AsyncClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_matrix_media(
    client: AsyncClient,
    event: object,
    matrix_files_dir: Path,
    workspace: Path,
    *,
    error_callback: Callable[[str], Awaitable[None]] | None = None,
) -> str | None:
    """Download media from a Matrix event, return agent prompt.

    Returns ``None`` if the download fails or the event has no media URL.
    *error_callback* is called with an error message on failure so the
    user gets feedback (analogous to Telegram's ``message.answer``).
    """
    await asyncio.to_thread(matrix_files_dir.mkdir, parents=True, exist_ok=True)

    try:
        info = await download_matrix_media(client, event, matrix_files_dir)
    except Exception:
        logger.exception("Failed to download Matrix media")
        if error_callback:
            await error_callback("Could not download that file.")
        return None

    if info is None:
        return None

    try:
        await asyncio.to_thread(_update_index, matrix_files_dir)
    except Exception:
        logger.warning("Index update failed", exc_info=True)

    return build_media_prompt(info, workspace)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def download_matrix_media(
    client: AsyncClient,
    event: object,
    base_dir: Path,
) -> MediaInfo | None:
    """Download a Matrix media event into *base_dir*/YYYY-MM-DD/.

    Returns ``None`` when the event has no downloadable URL.
    """
    from nio import DownloadError

    mxc_url: str | None = getattr(event, "url", None)
    if not mxc_url:
        return None

    # Determine file name and MIME type from event attributes
    body: str = getattr(event, "body", "") or ""
    source_info: dict = getattr(event, "source", {})
    content: dict = source_info.get("content", {}) if isinstance(source_info, dict) else {}
    event_info: dict = content.get("info", {}) if isinstance(content, dict) else {}
    mime: str = event_info.get("mimetype", "") or ""
    msgtype: str = content.get("msgtype", "") or ""

    # Derive file name
    file_name = _sanitize_filename(body) if body else ""
    if not file_name:
        ext = mimetypes.guess_extension(mime) if mime else None
        file_name = f"matrix_file{ext or ''}"

    # Derive MIME from msgtype if not in event info
    if not mime:
        mime = _mime_from_msgtype(msgtype, file_name)

    # Derive original_type (photo/audio/video/document)
    original_type = _original_type_from_msgtype(msgtype)

    # Download from homeserver
    dest = await asyncio.to_thread(_prepare_destination, base_dir, file_name)
    resp = await client.download(mxc=mxc_url, save_to=dest)

    if isinstance(resp, DownloadError):
        logger.error("Matrix download failed for %s: %s", mxc_url, resp.message)
        return None

    logger.info("Downloaded Matrix %s -> %s (%s)", original_type, dest, mime)

    # Extract caption: for Matrix, body is typically the filename, not a caption.
    # If msgtype is m.image/m.video/m.audio, body is usually just the filename.
    # We don't treat it as a user caption unless it differs from the file_name.
    caption: str | None = None
    if body and body not in (file_name, dest.name):
        caption = body

    return MediaInfo(
        path=dest,
        media_type=mime,
        file_name=dest.name,
        caption=caption,
        original_type=original_type,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MSGTYPE_MIME: dict[str, str] = {
    "m.image": "image/png",
    "m.audio": "audio/ogg",
    "m.video": "video/mp4",
}

_MSGTYPE_ORIGINAL_TYPE: dict[str, str] = {
    "m.image": "photo",
    "m.audio": "audio",
    "m.video": "video",
    "m.file": "document",
}


def _mime_from_msgtype(msgtype: str, file_name: str) -> str:
    """Derive MIME type from Matrix msgtype and filename."""
    mime = _MSGTYPE_MIME.get(msgtype, "")
    if not mime:
        mime = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    return mime


def _original_type_from_msgtype(msgtype: str) -> str:
    """Map Matrix msgtype to a generic media category."""
    return _MSGTYPE_ORIGINAL_TYPE.get(msgtype, "document")


def build_media_prompt(info: MediaInfo, workspace: Path) -> str:
    """Build the Matrix-specific prompt for a received media file."""
    return _build_media_prompt_generic(info, workspace, transport="Matrix")
