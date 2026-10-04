# Web conversation versions and usage

Additive P3 contract, based on backend ea68d611 (Codex 0.160.0). No model, memory or environment migration. No production deployment is implied by this PR.

- `PATCH /app/sessions/:id {title}` remains the rename contract (1–120 Unicode characters).
- `POST /app/sessions/:id/fork {request_id: UUID, message_id: integer, mode: "branch" | "edit" | "regenerate"}` copies a bounded immutable API prefix into a new API session and returns `{ok,created,duplicate}`. Does not call a model, send notifications or switch the globally active session. The browser uses ordinary `/app/send` afterward only for an explicitly requested edit/regeneration. A lost send response remains uncertain and is never automatically retried.
- Branch includes the selected row. Edit excludes the selected human prompt and everything later. Regeneration excludes the human prompt preceding the selected assistant answer and everything later. Original sessions and memory rows are unchanged. Copied rows omit generation identifiers and usage; `meta.branch_origin` marks historical copies. Attachment references are shared; existing deletion code removes only unreferenced files. Edit/regeneration with source prompt attachments is rejected until attachment resending is supported.
- A UUID-derived target plus SQLite receipt makes the copy idempotent. Reusing a UUID for a different source/point/mode fails. A process exit between data commit and session publication can resume the same receipt. Deleted target IDs cannot be resurrected.
- Both loop entrypoints share API copies under their existing session locks. Relay and loop each enforce their existing authentication. Up to 5000 rows / 8 MiB of history.
- Capability: `web_sessions.providers.api.message_fork=true`; Codex `message_fork` follows the three existing runtime gates. Codex sources use `thread/fork` with `lastTurnId`, durable provider/model/thread bindings and the same read-only/tool isolation profile. A completed answer branches through its native turn; edits and regenerations fork through the preceding turn. An empty prefix creates a pinned empty Codex session, whose first explicit send starts its native thread. No API fallback.
- A Codex user-message branch keeps the preceding completed turns and returns `draft` containing the question. It does not accidentally include that question's old answer or generate automatically. The UI explains this before confirmation.
- Native fork receipts in relay SQLite record dispatch before RPC. An unknown RPC result is never automatically repeated. A successful RPC is recorded before local message copying/publication, allowing recovery after a publication failure. Copied native turn bindings are separate from usage/jobs and support further branching even if the original visible conversation is removed. Original threads are never rolled back. The generation database's schema is unchanged.
- `/provider/status` keeps existing flattened primary quota fields and, when a secondary window exists, adds `rate_limits[].windows` with `window`, `used_percent`, `window_duration_mins`, `resets_at`. No credential or credit-balance projection. Clients identify a week by 10080 minutes, not primary/secondary position.
- `/provider/usage` remains the existing sanitized official usage endpoint.
- Chat Completions streaming asks for `stream_options.include_usage=true`. Strict older compatible services may explicitly set `LOOP_STREAM_INCLUDE_USAGE=false`; no automatic resubmission or fallback is added. Responses usage and completion usage already persist in `meta.api.usage`; missing usage remains unknown.

Validation includes isolated SQLite prefix copies, replays/conflicts, original preservation, role/provider checks, storage failure, quota filtering, payload opt-out and real P3 browser integration in the frontend PR. No production conversations or model accounts are used.

Primary protocol sources (checked 2026-10-04):
- https://developers.openai.com/codex/app-server
- https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
