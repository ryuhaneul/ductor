"""Detect how ductor was installed (pipx, uv tool, pip, or dev/source)."""

from __future__ import annotations

import json
import logging
import sys
from importlib.metadata import distribution
from typing import Literal

logger = logging.getLogger(__name__)

InstallMode = Literal["pipx", "uv", "pip", "dev"]

_PACKAGE_NAME = "ductor"


def detect_install_mode() -> InstallMode:
    """Detect installation method at runtime.

    Returns:
        ``"pipx"`` -- installed via ``pipx install ductor``
        ``"uv"``   -- installed via ``uv tool install ductor``
        ``"pip"``  -- installed via ``pip install ductor`` (from PyPI)
        ``"dev"``  -- editable install (``pip install -e .``) or running from source
    """
    prefix = str(sys.prefix).replace("\\", "/").lower()
    executable = str(getattr(sys, "executable", "")).replace("\\", "/").lower()

    if "pipx" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix or "/uv/tools/" in executable:
        return "uv"

    try:
        dist = distribution(_PACKAGE_NAME)
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            url_info = json.loads(direct_url_text)
            if url_info.get("dir_info", {}).get("editable", False):
                return "dev"
    except Exception:
        return "dev"

    return "pip"


def is_upgradeable() -> bool:
    """Return True if the bot can self-upgrade (pipx, uv, or pip; not dev)."""
    return detect_install_mode() != "dev"
