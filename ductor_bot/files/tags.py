"""File path tag parsing, MIME detection, and file classification."""

from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import filetype as _filetype

FILE_PATH_RE = re.compile(r"<file:([^>]+)>")

_SVG_SUFFIXES = frozenset({".svg", ".svgz"})


def extract_file_paths(text: str) -> list[str]:
    """Return all ``<file:/path>`` references from *text*."""
    return FILE_PATH_RE.findall(text)


def strip_file_tags(text: str) -> str:
    """Remove all ``<file:/path>`` tags from *text* and strip whitespace."""
    return FILE_PATH_RE.sub("", text).strip()


def path_from_file_tag(file_tag: str) -> Path:
    """Convert one ``<file:...>`` payload to a local filesystem path.

    Handles plain paths and ``file:`` URIs. On Windows it normalizes
    drive-letter variants such as ``/C/...`` and ``/C:/...``.
    """
    value = file_tag.strip()
    if not value:
        return Path(value)

    parsed = urlparse(value)
    if parsed.scheme == "file":
        if parsed.netloc and parsed.path:
            # file://server/share/path or file://C:/Users/...
            value = f"//{parsed.netloc}{parsed.path}"
        elif parsed.netloc:
            value = f"//{parsed.netloc}"
        else:
            value = parsed.path or ""

    value = unquote(value)
    if _is_windows():
        value = _normalize_windows_tag_path(value)
    return Path(value)


def guess_mime(path: Path | str) -> str:
    """Guess MIME type using magic bytes first, then extension fallback.

    Uses the ``filetype`` library for binary format detection (images, audio,
    video, archives).  Falls back to ``mimetypes`` for text-based formats
    (source code, SVG, plain text) that lack magic byte signatures.
    """
    kind = _filetype.guess(str(path))
    if kind is not None:
        return str(kind.mime)
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def classify_mime(mime: str) -> str:
    """Classify a MIME type string into a category.

    Returns ``"photo"``, ``"audio"``, ``"video"``, or ``"document"``.
    """
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "document"


def is_image_path(path_str: str) -> bool:
    """Check if a file path points to a raster image based on extension/MIME.

    Uses extension-based detection only (no file access).  For content-based
    detection on files that exist on disk, use ``guess_mime`` instead.
    """
    mime = mimetypes.guess_type(path_str)[0] or ""
    return mime.startswith("image/") and Path(path_str).suffix.lower() not in _SVG_SUFFIXES


def _is_windows() -> bool:
    """Return True when running on Windows."""
    return os.name == "nt"


def _normalize_windows_tag_path(value: str) -> str:
    """Normalize Windows drive-letter path variants from file tags."""
    path = value.replace("\\", "/")

    # file://C:/Users/... -> //C:/Users/... after URI parsing
    if len(path) >= 4 and path.startswith("//") and path[2].isalpha() and path[3] == ":":
        path = path[2:]

    # /C:/Users/... -> C:/Users/...
    if len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
        return path[1:]

    # /C/Users/... -> C:/Users/...
    if (
        len(path) >= 2
        and path[0] == "/"
        and path[1].isalpha()
        and (len(path) == 2 or path[2] == "/")
    ):
        tail = path[2:]
        if not tail:
            tail = "/"
        elif not tail.startswith("/"):
            tail = f"/{tail}"
        return f"{path[1]}:{tail}"

    return path
