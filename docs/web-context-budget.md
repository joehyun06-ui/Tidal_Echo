# Context budget observations · contract v1

Authenticated `GET /app/sessions/{id}/context-budget` forwards to the loop's authoritative session lookup. The response is private/no-store. It never invokes inference, resumes a native thread, compacts history or changes memory. Missing/deleted sessions return 404. Model limits are never inferred from model names or subscription quotas.

Codex's read-only notification scope observes `thread/tokenUsage/updated`, `item/started`, `item/completed`, and terminal turns. The pinned `rust-v0.160.0` schema defines `tokenUsage.last`, `tokenUsage.total`, and nullable `modelContextWindow`. This feature retains last input/output/total and the reported window, **not cumulative total usage**. Events must correlate with the latest job on an active bound native thread and its exact recorded turn ID. Pre-ACK events are omitted rather than guessing which turn owns them. Bounded integer counters only; arbitrary item contents, reasoning, tools and native identifiers never enter public responses.

`context_tokens` is the last reported total. `remaining_estimate = max(0, window - last_total)` and `used_percent_estimate = min(100, 100 * last_total / window)`. These are snapshots, not preflight admission promises. Unlike the CLI's baseline-adjusted user-controllable percentage, this raw ratio includes fixed overhead; the UI explicitly notes the difference. Missing data is null, not zero. `observed_at` is server receipt time. `stale` means the observation does not cover the latest canonical user message.

Only a `contextCompaction` lifecycle produces compaction state. Start/completion invalidate the previous occupancy estimate until new usage arrives. Completion is observed, never inferred from an RPC ACK. An unfinished compaction becomes unknown on terminal turn or process restart. No manual compaction RPC is added in this slice.

API mode reads the latest actual `api_generation` reply's provider-returned usage (`input_tokens`/`prompt_tokens`, `output_tokens`/`completion_tokens`, `total_tokens`). There is no API window denominator and therefore no percentage. Forked history does not count as a new inference. Missing usage stays unknown; input and output are not silently added to manufacture a missing total.

The additive `web_context_observations` table is per physical session/version, not per visible root. Forks and alternatives do not copy observations. Deletion purges the observation in the existing message deletion transaction; late notifications cannot recreate it after the canonical source is gone. This telemetry is separate from conversation preferences, billing and weekly subscription limits.

The PWA reads on opening the panel, on explicit refresh, and every five seconds while open. Closing invalidates pending requests; identity checks prevent old responses from painting a different session. Failed refresh clears visible observations instead of presenting old numbers as current. Private responses are not stored in Service Worker caches.

References checked 2026-10-12:
- https://developers.openai.com/codex/app-server (notification and compaction lifecycle)
- https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/app-server-protocol/schema/typescript/v2/ThreadTokenUsage.ts
- https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/protocol/src/protocol.rs (`tokens_in_context_window`, `percent_of_context_window_remaining`)

Next checklist slice: user-triggered compaction with durable asynchronous operation state and continuation packs. This release does not claim those controls are implemented.
