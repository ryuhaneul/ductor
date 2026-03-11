# heartbeat/

Periodic proactive check loop for active sessions.

## Files

- `heartbeat/observer.py`: `HeartbeatObserver` loop lifecycle + gating

## Public API (`HeartbeatObserver`)

- `set_result_handler(handler)`
- `set_heartbeat_handler(handler)`
- `set_busy_check(check)`
- `set_stale_cleanup(cleanup)`
- `start()`
- `stop()`

Helper:

- `utils/quiet_hours.py::is_quiet_hour(...)`

## Runtime flow

1. observer loop sleeps `interval_minutes`
2. skip full cycle during quiet hours (`user_timezone`)
3. per allowed user chat:
   - skip when busy check is true
   - execute heartbeat handler
   - deliver only non-empty results

`set_stale_cleanup(...)` is called before tick processing and is wired to `ProcessRegistry.kill_stale(...)`.

## Orchestrator contract

Observer delegates heartbeats to `Orchestrator.handle_heartbeat(...)` (`heartbeat_flow`):

- read-only active session lookup
- requires existing resumable session
- provider-match enforcement
- cooldown enforcement via `session.last_active`
- ACK-token suppression
- session metrics update only for non-ACK responses

## Delivery model

Heartbeat results are wrapped as envelopes and delivered through `MessageBus` -> active transport adapters (`TelegramTransport` / `MatrixTransport`).

No direct `TelegramBot._on_heartbeat_result` callback path exists anymore.
