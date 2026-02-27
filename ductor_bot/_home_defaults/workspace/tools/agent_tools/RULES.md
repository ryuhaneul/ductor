# Agent Management Tools

You are the **main agent** and can create, manage, and communicate with sub-agents.

## Available Tools

| Tool | Purpose |
|------|---------|
| `create_agent.py` | Create a new sub-agent (writes to `agents.json`, auto-detected) |
| `remove_agent.py` | Remove a sub-agent from the registry |
| `list_agents.py` | List all sub-agents and their configuration |
| `ask_agent.py` | Send a message to another agent and receive its response |

## Creating Sub-Agents

When creating a sub-agent:

1. The user **must provide** a Telegram bot token (created via @BotFather)
2. Choose a descriptive lowercase name (no spaces, e.g. `finanzius`, `codex`)
3. Configure provider and model based on the agent's purpose
4. The workspace is created automatically under `agents/<name>/`
5. The sub-agent starts automatically within seconds (FileWatcher)

```bash
python3 tools/agent_tools/create_agent.py \
  --name "agent-name" \
  --token "BOT_TOKEN" \
  --users "USER_ID1,USER_ID2" \
  [--provider claude] \
  [--model sonnet]
```

## Inter-Agent Communication

Use `ask_agent.py` to delegate tasks or ask questions to other agents.
Each agent has its own memory, workspace, and session — they are independent.

```bash
python3 tools/agent_tools/ask_agent.py "agent-name" "Your question or request here"
```

The response is printed to stdout. The target agent processes the message
in a one-shot CLI turn (no session state carried over).

## Removing Sub-Agents

Removing a sub-agent stops its Telegram bot but **preserves its workspace**.
The workspace can be reused if the agent is re-created with the same name.

```bash
python3 tools/agent_tools/remove_agent.py "agent-name"
```
