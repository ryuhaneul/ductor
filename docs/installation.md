# Installation Guide

## Requirements

1. Python 3.11+
2. `pipx` (recommended) or `pip`
3. At least one authenticated provider CLI:
   - Claude Code CLI: `npm install -g @anthropic-ai/claude-code && claude auth`
   - Codex CLI: `npm install -g @openai/codex && codex auth`
   - Gemini CLI: `npm install -g @google/gemini-cli` and authenticate in `gemini`
4. Telegram bot token from [@BotFather](https://t.me/BotFather)
5. Telegram user ID from [@userinfobot](https://t.me/userinfobot)
6. Docker optional (recommended for sandboxing)

## Install

### pipx (recommended)

```bash
pipx install ductor
```

### pip

```bash
pip install ductor
```

### from source

```bash
git clone https://github.com/PleasePrompto/ductor.git
cd ductor
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## First run

```bash
ductor
```

On first run, onboarding does:

- checks Claude/Codex/Gemini auth status,
- asks for Telegram token + user ID,
- asks timezone,
- offers Docker sandboxing,
- offers service install,
- writes config and seeds `~/.ductor/`.

If service install succeeds, onboarding returns without starting foreground bot.

## Platform notes

### Linux

```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm
pip install pipx
pipx ensurepath
pipx install ductor
ductor
```

Optional Docker:

```bash
sudo apt install docker.io
sudo usermod -aG docker $USER
```

### macOS

```bash
brew install python@3.11 node pipx
pipx ensurepath
pipx install ductor
ductor
```

### Windows (native)

```powershell
winget install Python.Python.3.11
winget install OpenJS.NodeJS
pip install pipx
pipx ensurepath
pipx install ductor
ductor
```

Native Windows is fully supported, including service management via Task Scheduler.

### Windows (WSL)

WSL works too. Install like Linux inside WSL.

```bash
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm
pip install pipx
pipx ensurepath
pipx install ductor
ductor
```

## Docker sandboxing

Enable in config:

```json
{
  "docker": {
    "enabled": true
  }
}
```

Notes:

- Docker image is built on first use when missing.
- Container is reused between calls.
- On Linux, ductor maps UID/GID to avoid root-owned files.
- If Docker setup fails at startup, ductor logs warning and falls back to host execution.

Docker CLI shortcuts:

```bash
ductor docker enable
ductor docker disable
ductor docker rebuild
```

- `enable` / `disable` toggles `docker.enabled` in `config.json` (restart bot afterwards).
- `rebuild` stops the bot, removes container + image, and forces fresh build on next start.

## Direct API server (optional)

Enable in config:

```json
{
  "api": {
    "enabled": true,
    "host": "0.0.0.0",
    "port": 8741,
    "token": "",
    "allow_public": false
  }
}
```

Notes:

- token is auto-generated and persisted on first API start when empty.
- endpoints:
  - WebSocket: `ws://<host>:8741/ws`
  - health: `GET /health`
  - file download: `GET /files?path=...` (Bearer token)
  - file upload: `POST /upload` (Bearer token, multipart)
- default API session maps to first `allowed_user_ids` entry; clients can override `chat_id` in auth payload.
- recommended deployment is a private network (for example Tailscale).

## Background service

Install:

```bash
ductor service install
```

Manage:

```bash
ductor service status
ductor service start
ductor service stop
ductor service logs
ductor service uninstall
```

Backends:

- Linux: `systemd --user` service `~/.config/systemd/user/ductor.service`
- macOS: Launch Agent `~/Library/LaunchAgents/dev.ductor.plist`
- Windows: Task Scheduler task `ductor`

Windows note:

- service install prefers `pythonw.exe -m ductor_bot` (no visible console window),
- installed Task Scheduler service uses logon trigger + restart-on-failure retries,
- some systems require elevated terminal permissions for Task Scheduler operations.

Log command behavior:

- Linux: live `journalctl --user -u ductor -f`
- macOS/Windows: recent lines from `~/.ductor/logs/agent.log` (fallback newest `*.log`)

## VPS notes

Small Linux VPS is enough. Typical path:

```bash
ssh user@host
sudo apt update && sudo apt install python3 python3-pip python3-venv nodejs npm docker.io
pip install pipx
pipx ensurepath
pipx install ductor
ductor
```

Security basics:

- keep SSH key-only auth
- enable Docker sandboxing for unattended automation
- keep `allowed_user_ids` restricted
- use `/upgrade` or `pipx upgrade ductor`

## Troubleshooting

### Bot not responding

1. check `telegram_token` + `allowed_user_ids`
2. run `ductor status`
3. inspect `~/.ductor/logs/agent.log`
4. run `/diagnose` in Telegram

### CLI installed but not authenticated

Authenticate at least one provider and restart:

```bash
claude auth
# or
codex auth
# or
# authenticate in gemini CLI
```

### Docker enabled but not running

```bash
docker info
```

Then validate `docker.enabled` + image/container names in config.

### Webhooks not arriving

- set `webhooks.enabled: true`
- expose `127.0.0.1:8742` through tunnel/proxy when external sender is used
- verify auth settings and hook ID

## Upgrade and uninstall

Upgrade:

```bash
pipx upgrade ductor
```

Uninstall:

```bash
pipx uninstall ductor
rm -rf ~/.ductor  # optional data removal
```
