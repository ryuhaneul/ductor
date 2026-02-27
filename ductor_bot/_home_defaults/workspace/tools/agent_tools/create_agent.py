#!/usr/bin/env python3
"""Create a new sub-agent by writing to agents.json.

The AgentSupervisor watches agents.json via FileWatcher and automatically
starts the new agent within seconds.

Usage:
    python3 create_agent.py --name NAME --token TOKEN --users ID1,ID2 [--provider P] [--model M]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _agents_path() -> Path:
    """Resolve agents.json path from DUCTOR_HOME or default."""
    import os

    home = Path(os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor")))
    return home / "agents.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new sub-agent")
    parser.add_argument("--name", required=True, help="Agent name (lowercase, no spaces)")
    parser.add_argument("--token", required=True, help="Telegram bot token")
    parser.add_argument("--users", required=True, help="Comma-separated allowed user IDs")
    parser.add_argument("--provider", default=None, help="AI provider (claude/codex/gemini)")
    parser.add_argument("--model", default=None, help="Model name (opus/sonnet/haiku/...)")
    args = parser.parse_args()

    # Validate name
    name = args.name.lower().strip()
    if not name or " " in name or name == "main":
        print(f"Error: Invalid agent name '{name}'. Must be lowercase, no spaces, not 'main'.", file=sys.stderr)
        sys.exit(1)

    # Parse user IDs
    try:
        user_ids = [int(uid.strip()) for uid in args.users.split(",") if uid.strip()]
    except ValueError:
        print("Error: User IDs must be integers.", file=sys.stderr)
        sys.exit(1)

    if not user_ids:
        print("Error: At least one user ID is required.", file=sys.stderr)
        sys.exit(1)

    # Load existing agents
    path = _agents_path()
    agents: list[dict] = []
    if path.is_file():
        try:
            agents = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            agents = []

    # Check for duplicate
    if any(a.get("name") == name for a in agents):
        print(f"Error: Agent '{name}' already exists.", file=sys.stderr)
        sys.exit(1)

    # Build agent entry
    entry: dict = {
        "name": name,
        "telegram_token": args.token,
        "allowed_user_ids": user_ids,
    }
    if args.provider:
        entry["provider"] = args.provider
    if args.model:
        entry["model"] = args.model

    agents.append(entry)

    # Write
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(agents, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Agent '{name}' created successfully.")
    print(f"  Token: {args.token[:8]}...")
    print(f"  Users: {user_ids}")
    if args.provider:
        print(f"  Provider: {args.provider}")
    if args.model:
        print(f"  Model: {args.model}")
    print(f"\nThe agent will start automatically within a few seconds.")


if __name__ == "__main__":
    main()
