"""Bot command definitions shared across layers."""

from __future__ import annotations

BOT_COMMANDS: list[tuple[str, str]] = [
    ("new", "Reset active provider session"),
    ("stop", "Stop the running agent"),
    ("status", "Show session info"),
    ("model", "Show/switch model"),
    ("memory", "Show main memory"),
    ("cron", "View/manage scheduled cron jobs"),
    ("info", "Docs, links & about"),
    ("upgrade", "Check for updates"),
    ("restart", "Restart bot"),
    ("session", "Run task in named background session"),
    ("sessions", "View/manage background sessions"),
    ("showfiles", "Browse ductor files"),
    ("diagnose", "Show system diagnostics"),
    ("help", "Show all commands"),
    ("agents", "List all agents"),
    ("agent_start", "Start a sub-agent"),
    ("agent_stop", "Stop a sub-agent"),
]
