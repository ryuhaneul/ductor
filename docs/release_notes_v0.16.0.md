# v0.16.0 Release Notes

Summary of the major changes between [`v0.15.0`](https://github.com/PleasePrompto/ductor/releases/tag/v0.15.0) and `v0.16.0`.

Source basis for this doc:

- `git log v0.15.0..v0.16.0 --oneline`
- `.planning/phases/02-task-hardening/PHASE-SUMMARY.md`
- `.planning/phases/03-provider-support/PHASE-SUMMARY.md`
- `.planning/phases/04-memory-subsystem/PHASE-SUMMARY.md`
- `.planning/phases/05-dx-polish/PHASE-SUMMARY.md`

## Highlights

- **Memory subsystem upgrade**
  - pre-compaction silent flush on `CompactBoundaryEvent`
  - configurable memory reflection hook
  - LLM-driven `MAINMEMORY.md` compaction with recency preservation
- **Task/runtime hardening**
  - `/new` now resets the configured default-provider bucket for the current session key
  - nested task execution preserves topic/thread routing
  - task cancellation kills the provider subprocess tree first
  - tool-only successful turns now emit a visible neutral status line
  - task registry persists `original_prompt` and warns when `TASKMEMORY.md` truncation would hide detail
- **Provider and model improvements**
  - Claude `sonnet[1m]` and `opus[1m]` models are available
  - Gemini thought markers are filtered out of user-visible output
  - Gemini API-key mode re-checks `settings.json` before emitting missing-key warnings
- **DX polish**
  - Telegram stage-based status reactions
  - configurable startup/upgrade notification routing
  - external audio/video transcription command hooks
  - background task priorities: `interactive`, `background`, `batch`
  - `ask_agent_async.py` supports `--reply-to` and `--silent`
- **Runtime/platform polish**
  - Matrix startup parity improvements (recovery + lifecycle notifications)
  - Matrix queue/dedup/drain fixes
  - Telegram channel allowlist support
  - `just check`, `just test`, and `just fix` workflows

## Behavior Shifts To Know

- `/new` is now a factory reset to the configured default model/provider for the current chat or topic. It no longer means "reset whichever provider bucket happens to be active right now."
- `scene.status_reaction` now defaults to `true`. On Telegram it takes precedence over `scene.seen_reaction` so both features do not fight over the same emoji slot.
- Startup and upgrade notifications can now be routed to specific chats/topics via `notifications.startup_targets` and `notifications.upgrade_targets`.
- Bundled media tools can delegate transcription to external commands via `transcription.audio_command` and `transcription.video_command`.

## Files Most Affected

- [`docs/config.md`](config.md)
- [`docs/architecture.md`](architecture.md)
- [`docs/system_overview.md`](system_overview.md)
- [`docs/modules/orchestrator.md`](modules/orchestrator.md)
- [`docs/modules/cli.md`](modules/cli.md)
- [`docs/modules/multiagent.md`](modules/multiagent.md)
- [`docs/modules/tasks.md`](modules/tasks.md)

## Verification Snapshot

At the end of the local milestone work captured in `.planning/`:

- `pytest` passed
- `ruff format --check .` passed
- `ruff check .` passed
- `mypy ductor_bot` passed
- `python -m ductor_bot.i18n.check` passed
