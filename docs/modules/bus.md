# bus/

Unified result-delivery layer for observers, async inter-agent responses, and task callbacks.

## Files

- `bus/envelope.py`: `Envelope`, `Origin`, `DeliveryMode`, `LockMode`
- `bus/bus.py`: `MessageBus` coordinator (`submit`, lock/inject/deliver pipeline)
- `bus/lock_pool.py`: shared per-session lock pool (`(chat_id, topic_id)`)
- `bus/adapters.py`: conversion helpers from domain results to `Envelope`
- `messenger/telegram/transport.py`: Telegram transport adapter + origin-specific formatting
- `messenger/matrix/transport.py`: Matrix transport adapter for room delivery formatting/routing

## Why this module exists

Before the refactor, delivery logic was split across multiple `deliver_*` paths and lock dicts.
`bus/` centralizes delivery into one pipeline:

1. convert result -> `Envelope`
2. optionally acquire shared session lock
3. optionally inject prompt into active session
4. deliver through registered transport(s)

## `Envelope` model

Core fields:

- identity: `origin`, `chat_id`, `topic_id`
- input for injection: `prompt`, `prompt_preview`
- output for delivery: `result_text`, `status`, `is_error`
- routing flags: `delivery`, `lock_mode`, `needs_injection`
- telegram metadata: `reply_to_message_id`, `thread_id`
- context: `provider`, `model`, `session_name`, `session_id`, `metadata`

Lock key: `envelope.lock_key -> (chat_id, topic_id)`.

## `MessageBus` flow

`submit(envelope)`:

1. assign `envelope_id` when missing
2. run optional audit hook
3. if `lock_mode=REQUIRED`: lock via shared `LockPool`
4. if `needs_injection`: call `SessionInjector.inject_prompt(...)` (orchestrator)
5. run optional pre-delivery hook
6. fan-out delivery to all registered transports

Registered transports: `TelegramTransport`, `MatrixTransport`.

## Adapter mapping (`adapters.py`)

- `from_background_result(...)`
- `from_cron_result(...)`
- `from_heartbeat(...)`
- `from_webhook_cron_result(...)`
- `from_webhook_wake(...)`
- `from_interagent_result(...)`
- `from_task_result(...)`
- `from_task_question(...)`
- `from_user_message(...)` (audit-only envelope)

Task/topic nuance:

- task result envelopes map `thread_id -> topic_id`
- task question envelopes also carry `topic_id`
- injected responses route back into the originating forum topic session

## Wiring

- Single-transport mode: the active bot creates `MessageBus(lock_pool=self._lock_pool)` and registers its transport
- Multi-transport mode: `MultiBotAdapter` creates one shared `MessageBus`; each bot registers its own transport adapter
- `run_startup()` calls `orch.wire_observers_to_bus(bot._bus, wake_handler=...)`
- `ObserverManager.wire_to_bus(...)` connects cron/heartbeat/background/webhook callbacks in one call
- `bus.set_injector(orchestrator)` enables prompt injection paths

## Locking model

A single `LockPool` is shared by:

- `SequentialMiddleware` (Telegram ingress)
- `ApiServer` (WebSocket per-session locking)
- `MessageBus` (observer/result routing)

This prevents lock-scope drift across transports and background delivery paths.
