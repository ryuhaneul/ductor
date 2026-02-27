#!/usr/bin/env python3
"""Send a message to another agent via the InterAgentBus.

Uses the internal localhost HTTP API to communicate with the bus.
Environment variables DUCTOR_AGENT_NAME and DUCTOR_INTERAGENT_PORT
are automatically set by the Ductor framework.

Usage:
    python3 ask_agent.py TARGET_AGENT "Your message here"
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python3 ask_agent.py TARGET_AGENT \"message\"", file=sys.stderr)
        sys.exit(1)

    target = sys.argv[1]
    message = sys.argv[2]
    port = os.environ.get("DUCTOR_INTERAGENT_PORT", "8799")
    sender = os.environ.get("DUCTOR_AGENT_NAME", "unknown")

    url = f"http://127.0.0.1:{port}/interagent/send"
    payload = json.dumps({"from": sender, "to": target, "message": message}).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(f"Error: Cannot reach inter-agent API at {url}: {e}", file=sys.stderr)
        print("Make sure the Ductor supervisor is running with multi-agent support.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if result.get("success"):
        print(result.get("text", ""))
    else:
        error = result.get("error", "Unknown error")
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
