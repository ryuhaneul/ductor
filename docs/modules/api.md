# api/

Direct WebSocket API server for app connections without Telegram.

## Purpose

Provides a transport-independent interface to the orchestrator over WebSocket. Designed for use over Tailscale or other private networks so that no message content passes through third-party servers (unlike Telegram).

All WebSocket communication after the handshake is end-to-end encrypted using NaCl Box (Curve25519-XSalsa20-Poly1305). Even with Tailscale or a reverse proxy in between, neither can read message content.

Runs alongside the Telegram bot without affecting it. Both transports share the same orchestrator, CLI service, and session system.

## Files

- `crypto.py`: E2E encryption session (NaCl Box, ephemeral keypairs, encrypt/decrypt)
- `server.py`: aiohttp WebSocket server, auth + key exchange, encrypted session loop, streaming dispatch, file download/upload

## Config

Section `api` in `config.json`:

| Field | Default | Description |
|---|---|---|
| `enabled` | `false` | Start the API server |
| `host` | `0.0.0.0` | Bind address |
| `port` | `8741` | Bind port |
| `token` | `""` | Auth token (auto-generated on first API start) |
| `chat_id` | `0` | Present in schema; currently not consumed by orchestrator startup defaulting |
| `allow_public` | `false` | Suppress Tailscale-not-detected warning |

File-related config in `cleanup` section:

| Field | Default | Description |
|---|---|---|
| `api_files_days` | `30` | Retention period for uploaded API files |

Retention implementation detail:

- cleanup currently checks top-level files only (non-recursive),
- API uploads are stored under `api_files/YYYY-MM-DD/`, so those uploaded files are currently not deleted by cleanup.

## Authentication + E2E key exchange

Authentication and E2E setup happen in a single handshake:

1. Token is auto-generated (`secrets.token_urlsafe(32)`, 256-bit entropy) on first API start and persisted to config.
2. Client must send an auth message as the first WebSocket frame, including its ephemeral public key (`e2e_pk`).
3. Token comparison uses `hmac.compare_digest` (constant-time).
4. Server generates its own ephemeral keypair, computes the shared key, and responds with its public key.
5. After `auth_ok`, all subsequent WebSocket frames are NaCl Box encrypted.
6. Auth timeout: 10 seconds.

### HTTP endpoint auth

File download and upload endpoints use `Authorization: Bearer <token>` header authentication with the same token as WebSocket auth. HTTP endpoints are not E2E encrypted (use HTTPS/Tailscale for transport security).

## E2E encryption

### Crypto primitives

- **Key exchange**: Curve25519 (ephemeral keypairs per WebSocket session)
- **Authenticated encryption**: XSalsa20-Poly1305
- **Implementation**: PyNaCl (server), tweetnacl (app)

### Forward secrecy

Each WebSocket connection generates a fresh keypair. Compromising one session's keys does not affect past or future sessions.

### Wire format

All post-auth WebSocket frames use a single format:

```
base64(nonce_24 + ciphertext_with_mac)
```

- `nonce_24`: 24-byte random nonce (unique per message)
- `ciphertext_with_mac`: NaCl Box output (16-byte Poly1305 MAC + encrypted payload)

The encrypted payload is compact JSON (`separators=(",", ":")`).

### Interop details (PyNaCl <-> tweetnacl)

**Server encrypt (Python)**:
```python
encrypted = box.encrypt(plaintext)        # returns nonce + ciphertext
frame = base64.b64encode(bytes(encrypted))
```

**App decrypt (TypeScript)**:
```typescript
const raw = decode(frame);                // base64
const nonce = raw.slice(0, 24);
const ct = raw.slice(24);
const pt = nacl.box.open.after(ct, nonce, sharedKey);
```

**App encrypt (TypeScript)**:
```typescript
const nonce = nacl.randomBytes(24);
const ct = nacl.box.after(plaintext, nonce, sharedKey);
const combined = new Uint8Array(24 + ct.length);
combined.set(nonce); combined.set(ct, 24);
frame = encode(combined);                 // base64
```

**Server decrypt (Python)**:
```python
raw = base64.b64decode(frame)
plaintext = box.decrypt(raw)              # reads nonce from first 24 bytes
```

### Error handling

If decryption fails (tampered data, wrong key), the server responds with an encrypted `decrypt_failed` error (the server's encrypt direction is still valid):

```
Server <- encrypted({"type": "error", "code": "decrypt_failed", "message": "Decryption failed"})
```

## Session identity (`chat_id`)

The session system is keyed by `chat_id`. This controls which conversation history, provider state, and memory the API client uses.

- startup default: first `allowed_user_ids` entry (fallback `1` if allowlist is empty).
- auth override: client may pass `chat_id` in the auth payload for an isolated session.

Current runtime note: `config.api.chat_id` exists in `AgentConfig` but is not used in `Orchestrator._start_api_server()`.

## WebSocket protocol

Endpoint: `ws://<host>:<port>/ws`

### Handshake (plaintext)

```
Client -> {"type": "auth", "token": "...", "e2e_pk": "<base64>", "chat_id": 12345}
Server <- {"type": "auth_ok", "chat_id": 12345, "e2e_pk": "<base64>"}
```

`chat_id` in the auth message is optional. Omitting it uses the orchestrator-provided default (first allowlisted user, fallback `1`).

`e2e_pk` is mandatory. Must be a base64-encoded 32-byte Curve25519 public key. The server rejects connections without it.

`auth_ok` is the last plaintext message. Everything after is encrypted.

### Sending messages (encrypted)

```
Client -> encrypted({"type": "message", "text": "hello"})
```

Server streams back encrypted events:

```
Server <- encrypted({"type": "text_delta",     "data": "response chunk"})
Server <- encrypted({"type": "tool_activity",  "data": "Reading file..."})
Server <- encrypted({"type": "system_status",  "data": "Thinking"})
Server <- encrypted({"type": "result",         "text": "full response", "stream_fallback": false, "files": [...]})
```

`result` is always the final event for a message. `text` contains the complete response.

#### File references in results

When the CLI response contains `<file:/path>` tags, the `result` event includes a `files` array:

```json
{
  "type": "result",
  "text": "Here is the chart <file:/home/user/.ductor/workspace/files/2025-06-15/chart.png>",
  "stream_fallback": false,
  "files": [
    {
      "path": "/home/user/.ductor/workspace/files/2025-06-15/chart.png",
      "name": "chart.png",
      "is_image": true
    }
  ]
}
```

The client can use the `path` value to download the file via `GET /files`.

### Aborting (encrypted)

```
Client -> encrypted({"type": "abort"})
Server <- encrypted({"type": "abort_ok", "killed": 1})
```

Sending `/stop` as message text has the same effect.

### Errors

Auth-phase errors are plaintext:

```
Server <- {"type": "error", "code": "auth_failed", "message": "..."}
```

Post-auth errors are encrypted:

```
Server <- encrypted({"type": "error", "code": "...", "message": "..."})
```

Error codes:

| Code | Phase | Description |
|---|---|---|
| `auth_timeout` | auth | No auth message within 10 s |
| `auth_required` | auth | First message must be auth JSON |
| `auth_failed` | auth | Invalid token or e2e_pk |
| `decrypt_failed` | session | E2E decryption failed |
| `empty` | session | Empty message text |
| `unknown_type` | session | Unknown message type |
| `no_handler` | session | Message handler not configured |
| `internal_error` | session | Orchestrator exception |

## HTTP endpoints

### `GET /health`

Health check. No authentication required.

```json
{"status": "ok", "connections": 1}
```

### `GET /files?path=<path>`

Download a file from the server filesystem.

**Auth**: `Authorization: Bearer <token>`

**Query parameters**:

| Param | Required | Description |
|---|---|---|
| `path` | yes | File path on the server (absolute path recommended) |

**Responses**:

| Status | Description |
|---|---|
| `200` | File contents with detected `Content-Type` |
| `400` | Missing `path` query parameter |
| `401` | Missing or invalid Bearer token |
| `403` | Path outside allowed roots (`file_access` config) |
| `404` | File not found |

MIME type is detected using magic bytes (`filetype` library), falling back to extension-based detection for text files.

**Example**:

```bash
curl -H "Authorization: Bearer <token>" \
  "http://localhost:8741/files?path=/home/user/.ductor/workspace/files/2025-06-15/chart.png" \
  -o chart.png
```

### `POST /upload`

Upload a file to the server. The file is saved to the API files directory (`~/.ductor/workspace/api_files/YYYY-MM-DD/`).

**Auth**: `Authorization: Bearer <token>`

**Body**: `multipart/form-data`

| Field | Required | Description |
|---|---|---|
| `file` | yes | The file to upload (must be first multipart field) |
| `caption` | no | Text message to attach to the file (read from next multipart field when named `caption`) |

**Max size**: 50 MB

**Response** (`200`):

```json
{
  "path": "/home/user/.ductor/workspace/api_files/2025-06-15/photo.jpg",
  "name": "photo.jpg",
  "mime": "image/jpeg",
  "size": 245760,
  "prompt": "[INCOMING FILE]\nThe user sent you a file via API.\n..."
}
```

The `prompt` field contains the formatted prompt that can be sent to the orchestrator via WebSocket message. The `mime` field is detected from file content using magic bytes.

**Error responses**:

| Status | Description |
|---|---|
| `400` | Missing multipart body or `file` field |
| `401` | Missing or invalid Bearer token |
| `413` | File exceeds 50 MB limit |
| `503` | File uploads not configured |

**Example**:

```bash
curl -X POST -H "Authorization: Bearer <token>" \
  -F "file=@photo.jpg" \
  -F "caption=Look at this" \
  http://localhost:8741/upload
```

## File type detection

The API uses the `filetype` library for content-based file type detection via magic bytes. This covers all common binary formats (images, audio, video, archives, documents). For text-based formats (source code, plain text, SVG) that lack magic byte signatures, it falls back to `mimetypes` (extension-based).

File classification categories:

| Category | MIME prefix | Examples |
|---|---|---|
| `photo` | `image/*` | JPEG, PNG, GIF, WebP, BMP, HEIC, AVIF |
| `audio` | `audio/*` | MP3, OGG, AAC, FLAC, WAV |
| `video` | `video/*` | MP4, WebM, MOV, AVI, MKV |
| `document` | everything else | PDF, ZIP, text, code files |

## Orchestrator commands

Commands registered in the orchestrator's command registry work automatically:

- `/new` -- reset active provider session
- `/status` -- show session status
- `/model` / `/model <name>` -- show or switch model
- `/memory` -- memory operations
- `/cron` -- cron management
- `/diagnose` -- diagnostics
- `/upgrade` -- returns upgrade-check text, but upgrade action buttons are Telegram callback-based (`upg:*`)

These are sent as encrypted messages (`encrypted({"type": "message", "text": "/status"})`).

Commands handled exclusively by Telegram (`/start`, `/help`, `/info`, `/showfiles`, `/restart`) are not available through the API.

## Startup flow

In `Orchestrator.create()`, after all other observers:

1. Check `config.api.enabled`.
2. Auto-generate token if empty, persist to config.
3. Resolve default `chat_id` from first `allowed_user_ids` entry (fallback `1`).
4. Create `ApiServer`, wire `handle_message_streaming` and `abort` as callbacks.
5. Wire file context (`allowed_roots`, `upload_dir`, `workspace`).
6. Start server.
7. If no Tailscale detected and `allow_public=false`: log warning (server still starts).

Note: `config.api.chat_id` is currently not wired into this startup default.

## Shutdown

`Orchestrator.shutdown()` stops the API server first (closes all WebSocket connections), then proceeds with other observer shutdown.

## Sequential processing

Each `chat_id` gets an `asyncio.Lock`. Messages from the same session are processed sequentially, same as the Telegram `SequentialMiddleware` pattern. Different sessions can process concurrently.

## Disconnect handling

If the WebSocket connection drops mid-stream:

1. Streaming callbacks detect the closed connection and become no-ops.
2. After the orchestrator call completes, the server detects the disconnect.
3. Active CLI processes for that `chat_id` are aborted.

## Security model

### Transport layer

Default assumption: Tailscale (private WireGuard mesh).

- Both the server host and client device must be in the same Tailscale network.
- Traffic is encrypted by WireGuard at the transport level.
- No ports exposed to the public internet.
- The API server checks for Tailscale at startup and warns if not detected.

For public exposure (VPS, Cloudflare Tunnel, etc.): set `allow_public: true` and ensure HTTPS termination upstream (e.g. reverse proxy with TLS).

### Application layer (E2E)

Independent of the transport layer, all WebSocket message content is E2E encrypted:

- Ephemeral Curve25519 keypairs per connection (forward secrecy).
- NaCl Box (XSalsa20-Poly1305) authenticated encryption.
- Random 24-byte nonces per message (no nonce reuse).
- Neither Tailscale, a reverse proxy, nor any intermediary can read message content.
- Tampered ciphertext is rejected (Poly1305 MAC verification).

### What E2E covers

| Data path | Encrypted |
|---|---|
| WebSocket messages (post-auth) | E2E (NaCl Box) |
| WebSocket handshake (auth + auth_ok) | Transport only (Tailscale/TLS) |
| HTTP file download (`GET /files`) | Transport only (Tailscale/TLS) |
| HTTP file upload (`POST /upload`) | Transport only (Tailscale/TLS) |
| HTTP health check (`GET /health`) | None (public) |

## Architecture

```
App (Android/PWA)
  |
  | WebSocket (E2E encrypted, transport via Tailscale/TLS)
  v
ApiServer (aiohttp)
  |  _SecureChannel (decrypt incoming, encrypt outgoing)
  |
  | handle_message_streaming() callback
  v
Orchestrator
  |
  v
CLIService -> provider subprocess (claude/codex/gemini)
```

No Telegram involvement. The API server has zero imports from `ductor_bot.bot`.
