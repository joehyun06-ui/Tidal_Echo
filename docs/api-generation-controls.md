# API Web generation controls (2026-10-07)

This completes the API portion of wishlist stage 2. P3 Web API sessions now use
the same authenticated status/stop endpoints and snapshot envelope as Codex:

- `GET /app/sessions/{id}/generation`
- `POST /app/sessions/{id}/generation/stop {"generation_id":"api-gen-N"}`

The API capability declares `generation_controls` only with the loop brain, and
`steer: false`. Session provider authority remains immutable. Telegram, Kelivo,
memory provider dispatches, dry probes, older entrypoints and Codex native turns
retain their existing paths. API requests still use the original context builder,
route chain, timeouts and Chat Completions / Responses parsers.

## Admission, stopping and recovery

An additive `api_web_generations` table stores bounded metadata beside relay
messages; `api_web_closed_sessions` prevents late requests resurrecting a deleted
session. Canonical Web input and the queued run commit atomically before dispatch.
Only one active run per session and 32 across sessions may be admitted. Internal
ingest is conditional and idempotent. The canonical text, session and provenance
are checked before a task starts; image-only input remains valid.

Queued cancellation prevents provider dispatch. Running cancellation cancels the
specific asyncio provider task and exits its existing HTTPX stream context.
`stopping` is an ACK, not completion. Partial public text is persisted once after
the task exits, with `finish_reason: interrupted`. Empty cancellation creates no
assistant message. A normal completion that already won the race stays completed.
Duplicate or late stop never targets a later turn.

This is **local HTTP request cancellation**, not a universal provider cancellation
API or a billing guarantee. Responses declare `cancel_scope: local_request` and
`upstream_result_unknown`; the PWA says “已停止接收”. No background Responses mode
or new provider endpoint is enabled. HTTPX context closing contract:
https://www.python-httpx.org/async/#streaming-responses

Snapshots are in-memory only, at most 64,000 characters per active run. The status
GET repairs missing frames; the relay additionally emits ordinary API deltas for
already installed older PWA versions. Canonical final text, usage, actual route
model and server elapsed time commit in one transaction. Notification loss is
recovered by history GET, never another model call. Missing usage remains unknown.

Process restart marks old active runs failed with an explicit uncertainty flag;
it never repeats them. A queued dispatch not claimed in 45 seconds expires and
cannot later be started. A failure after partial output saves that text marked
incomplete. The user may inspect it and explicitly send/regenerate afterward.
Active jobs block deletion; successful deletion also removes run metadata.

## Validation and rollout

Isolated SQLite/fake-model tests cover exact/duplicate/queued stop, completion
races, restart, dispatch expiry, busy rejection before input persistence, source
validation, deletion, image-only input, partial failures and notification loss.
Browser tests cover API and Codex in Chromium/WebKit. The real P3 integration uses
only a local synthetic SSE model: reload restores a missing snapshot, stopping
closes the provider connection, and history contains one partial answer.

Publish the backend first, then the matching PWA. Rollback can leave the additive
tables in place; no existing schema, memory policy or model pin is changed. Do not
claim stage 2 fully complete: steering and richer public process/tool summaries
remain separate work. Stage 3 is the next larger context/continuity package.
