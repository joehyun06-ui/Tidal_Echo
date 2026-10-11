# Manual compaction and continuation extracts

Authenticated Web routes (all responses `private, no-store` at the relay):

| Route | Contract |
|---|---|
| `GET /app/sessions/{id}/compact` | Provider capability and latest durable operation, without native IDs or inference. |
| `POST /app/sessions/{id}/compact` | `{request_id: UUID}` reserves a single Codex compaction; no inline native call. |
| `POST /app/sessions/{id}/compact/reconcile` | Same receipt UUID; schedules history reconciliation of an unknown operation, never replays compaction. |
| `GET /app/sessions/{id}/continuation` | Editable extract of enabled conversation settings and up to 24 recent messages from this exact physical session. No model or memory calls. |

The existing generation worker owns compaction and normal generation sequentially.
The pinned Codex 0.160.0 `thread/compact/start` RPC returns an acknowledgement;
completion requires the matching `contextCompaction` item and terminal turn.
Each request UUID is durable. Dispatched work becomes unknown on timeout, transport
failure or restart. It is never retried automatically. Pending/unknown compaction
blocks generation admission, native fork, model edits and retirement for that
session. Other sessions may enqueue work for the same worker. Unknown results may
remain unknown when bounded native history does not provide sufficient evidence;
the user can take an editable continuation extract to a new session.

Compaction retains original relay chat history. No native summary, hidden reasoning
or tool content is persisted or shown. The previous budget observation is invalidated;
the next normal turn can supply a new estimate. This is not an API-provider compactor,
an automatic memory summary, or a guarantee of lossless model context.

Continuation excerpts carry message IDs/timestamps and mark per-message truncation,
omitted older history and excluded attachments. Disabled setting values and attachment
URLs/metadata are excluded. The draft is bounded to the composer's 12,000 UTF-16 units.
The PWA lets the user edit or copy it, then creates a same-provider session and stores
only a local draft. Creating the draft does not send a message. The new session uses
its own model settings; no native thread, hidden state or memory policy is copied.

Tests use synthetic SQLite messages and a fake native transport. Browser and real
P3 relay integration run in CI; no production account inference is used for validation.
