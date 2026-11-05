# GM-02L recoverable whole API version-group erasure

Default-off, non-production continuation of GM-02K. The explicit in-process
candidate closes and logically erases one plain API root and all its internal
alternatives, including nested alternatives. Independent explicit branches and
other groups retain their rows, messages, contexts and fork fingerprints.
No inference, sending, live chat deletion, G3 write, native work or HTTP wiring
is performed by this change.

## Entry, authorization and preflight

web_message_versions.delete_versions accepts an explicitly injected
CoordinatedApiGroupDeleteCommands dependency, the same session RLock, matching
LOOP_CONFIG, declared relay_db and actual canonical database path. Candidate
parameters without the dependency refuse; the existing default legacy path is
unchanged. Non-null external cleanup dependencies refuse before any erasure.

snapshot_group_delete is query-only: no table initialization, JSON mutation,
intent or receipt. Its packet contains generation, metadata/graph/group-state
digests, the complete sorted original group IDs, exact message IDs, referenced
message IDs and the expected retained-row digest. Plaintext, settings and routes
are not returned. The packet must be privately retained unchanged with the
request ID, root, principal, explicit UTC deleted_at and next_active.
It is an internal trusted contract, not authentication or proof of user intent.

Global/root/fallback scopes are checked before observation. Discovery reads the
bounded identifier graph under root scope, then authorizes every original member
before history/config normalization and content access. Write, replay and
recovery require current authorization for every original member and fallback.
Guards repeat inside the gate/transaction and before publication.

GM-02J validates API-only live members, ancestry, keys/modes, stored selection,
known job states, closure and group MCP action references. The candidate also
checks every version/selection column for references crossing group boundaries.
Malformed, cyclic, unpublished or partly deleted legacy groups refuse; they are
not adopted or repaired. The root must be a logical root, not an alternative.

At least one live API survivor must remain. Deleting an active member requires
an explicit live API fallback outside the group; another group's alternative is
not a valid fallback. There is no automatic conversation allocation. Group size
is capped by the existing 512-session limit, total target messages by 5000,
each auxiliary target table by 5000 and hashed payloads by the existing 8 MiB
serializer limit. IN queries stay within legacy SQLite bind limits.

## One SQL transaction, one recoverable JSON publication

The pure buffered callback removes all original raw group rows and appends one
minimal API tombstone per member with the same supplied deleted_at. It changes
active_session only to the supplied fallback. Outside raw rows/extensions,
routes/settings and old tombstones are preserved. No per-member legacy delete
or provider allocation is called.

Inside BEGIN IMMEDIATE the complete graph, metadata, group membership, content,
auxiliary rows, table presence and referenced-message plan are recomputed and
compared with the original packet. Then the same transaction commits:

- durable API closed markers for all original member IDs;
- deletion of group context, observations, fork fingerprints and settled jobs;
- removal of all group version edges and its root selection;
- deletion of unreferenced messages and redaction of referenced message IDs;
- the coordinator pending intent for the staged whole-group JSON.

Referenced messages retain ID, timestamp and direction with kind=deleted,
empty text and fixed deleted metadata. Explicit and implicit/case-insensitive
message FKs are recognized, avoiding cascading unrelated referencing rows.
Unknown incoming metadata FKs refuse, even if the referencing table is empty.
Triggers or unexpected known schemas refuse. SQL errors, authorization loss
before commit or process exit during erasure roll back all effects and intent.

The real API Store.accept checks every closed ID even while the old JSON is
still published. Unpublished recovery does not rediscover membership after
edges have been removed: it verifies the exact original group packet, absence
of any graph/selection references, all markers, no group content/auxiliary rows
and exact retained redacted rows. Restored data, new ingress/context, lost
markers or changed retained rows refuse publication without deleting again.

If JSON already has the target hash, recovery only finalizes that historical
intent. Committed replay returns its original generation without reapplying
CAS or erasure. Later bypass content is preserved; the receipt has only
generation/replayed and never claims current or global content_deleted.
Changed actor, time, fallback, group or digest conflicts. Concurrent identical
requests erase once; another stale request loses CAS.

## Restrictions and remaining integration

Only canonical plain API content is supported. Attachments, external cleanup,
MCP settings/actions, lifecycle records, channel/native mappings and known
ledger references refuse. Separate native stores, unknown/external stores,
independent fork copies and G3 data are not erased. SQL logical deletion does
not promise forensic removal from WAL, free pages, backups or journals.

SQLite effects/intent are atomic; SQLite and JSON/runtime are not. Between SQL
commit and JSON publication, old config rows can remain visible after version
relationships have disappeared. Legacy readers may temporarily interpret
children as standalone rows. Ordinary/manual writers can bypass the shared
gate. This cannot be enabled live until reader/writer/ingress/runtime fences,
real authorization and private packet persistence are installed and accepted.

Authorization revocation or damage after intent can leave irreversible SQL
erasure with pending JSON. Recovery cannot restore removed bodies, and no
automatic compensation, abort, orphan cleanup or blind repair is added.
Historical receipts do not prove stream/tool completion, consumed context,
G3 source/revision truth or live runtime success.

Keep production_supported, http_integrated, all_writers_integrated,
native_integrated, cross_store_atomic, external_cleanup_supported and
g3_delete_supported False. No merge, deployment, production data/JSON/env/WAL
change, service restart or model API call is authorized by this candidate.
ProjectionCard remains authority=derived; commercial C is not a daily
dependency. Frozen 36/36, 108/108 and historical OpenAI 7/7 are unchanged.

## Validation

37 new synthetic offline tests plus 492 prior focused tests: 529/529 passed.
Coverage includes actual nested legacy groups and the explicit entry, read-only
privacy, whole SQL ordering, independent branch preservation, all-member
authorization/revocation, CAS, closure/cross-root protection, FK redaction,
settled/uncertain jobs, unsupported references, payload bounds, mid-transaction
process exit, before/after JSON crashes, real closed-ID ingress rejection,
damage/new-data refusal, exact recovery, historical replay and process races.
The GitHub Python CI installs the full HTTP/native test dependencies; the
local focused run does not substitute for that complete discovery.
