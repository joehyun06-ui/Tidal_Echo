# Web conversation context · contract v1

The P3 production entrypoint exposes authenticated `GET` and `PATCH /app/sessions/{id}/context`, forwarding to the existing internal loop authority. Responses are private/no-store. Missing, deleted or invalid sessions cannot read or update settings. No MCP, memory or provider permission is added.

PATCH is a full replacement `{revision, config}`. GET returns `{ok, contract_version: 1, root_id, revision, updated_at, config}`. Revision 0 means defaults; concurrent/stale saves return 409 without overwriting. Validation failures return 400. There is no automatic write retry.

`config` has three boolean switches: `state_enabled`, `relationship_enabled`, `time_enabled` (default true), plus these strings (default empty):

| Fields | Maximum characters |
| --- | --- |
| user_name, assistant_name, mode, timezone | 80 each |
| relationship, tone, avoid | 500 each |
| recent, project, agreements | 1000 each |

Timezone is an IANA zone; blank uses the sending device's `/app/send.timezone`, with UTC as the fallback. The server supplies its receive time, corresponding local time/offset, the previous selected-history message time/role, and nonnegative elapsed seconds. Missing or invalid prior timestamps do not produce invented intervals. DST uses zoneinfo. This is timing context, not a sleep/emotion inference.

Settings live in the additive `web_session_context` table in the existing relay SQLite database. Message alternatives resolve to the visible conversation root. Explicit branches copy once and then diverge; replaying a fork never overwrites the branch's edits. Conversation deletion purges its settings in the existing message-purge transaction; independent branches retain their copy.

The P3 relay freezes enabled settings and server time into trusted canonical message metadata `web_context_v1` before dispatch. Raw message text is untouched; values in disabled sections are omitted (null). Editing settings while a response is running applies to later accepted messages. API input gets one current user-background message immediately before the raw user input; existing persona/history/image handling remains. Codex's canonical input includes the same background before the current text, and its existing input digest covers that frozen background and images. Its persona hash, account/model/session gates, RPC allowlist and tool isolation remain unchanged.

Old messages without a snapshot preserve their original protocol and digest. New snapshots explicitly replace earlier configuration hints, including when disabled; previously sent context can remain in provider conversation history. This does not erase chat history, promise provider-side forgetting, or write these settings into long-term memory. New independent conversations start with defaults; cross-window continuation packs are a later checklist item.

Verification uses isolated SQLite, synthetic messages, fake Codex transport and the real P3 relay against a synthetic API model. No production account inference is needed.
