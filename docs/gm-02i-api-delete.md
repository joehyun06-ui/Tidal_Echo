# GM-02I canonical plain-API erasure and recoverable tombstone candidate

Default-off, non-production continuation of GM-02H. All executed tests use
synthetic private databases/configs. No live conversation is deleted, created or
sent to; no model, G3 write, native service, HTTP install or deployment runs.

## Problem, scope and existing entry

Legacy deletion purges canonical messages, then updates JSON/tombstones, and its
duplicate path may assert content_deleted merely because a tombstone exists.
It can also allocate a replacement chat when removing the last API row.
This explicit candidate commits a durable API closed marker, scoped canonical
content cleanup and coordinator pending intent in one SQLite transaction, then
recovers the fixed JSON tombstone. It never creates a fallback chat or returns
the legacy content_deleted/UI deletion response.

Only plain API sessions are supported. A surviving API row is required; deleting
the last API refuses. If removing stored active, an explicitly authorized existing
API next_active is required. Otherwise None preserves stored active exactly.
A supplied next_active must be another live API row. No native qualification or
fallback allocation occurs. Explicit deleted_at is a fixed UTC timestamp, not a
clock call inside mutation.

web_session_delete.delete_conversation accepts an explicit dependency, the exact
injected session RLock, original request packet and resource paths. LOOP_CONFIG,
declared relay_db and actual canonical PRAGMA database_list must agree.
Candidate parameters without a dependency refuse before the ordinary branch.
Default ordinary calls retain their behavior. upload_dir, codex_store and
usage_ledger must all be None for this composition: configured external cleanup
refuses before erasure. HTTP/native/usage-ledger composition remains unfinished.

Attachments, MCP action/settings rows, version relationships/selections, lifecycle
records, channel jobs/conversations, known native or usage-ledger references are
refused. Target references in those known optional tables are inspected without
initializing them. Absent optional tables remain absent at preflight.
This is not exhaustive discovery of all external/unknown private copies.

## Scoped preflight and retained request

snapshot_delete requires global, target and optional next-active authorization
under a distinct read action. All scopes are checked before historical-provider
callbacks. Write/recovery uses a separate delete action and current scopes.
Checks recur inside the cooperative gate, after SQL cleanup before intent commit
and before publication/recovery. ACL/history is still external trusted code,
not an atomically integrated production identity service.

The query-only preflight returns generation, metadata/state/retained digests,
target message IDs and referenced IDs requiring redaction; no text, settings,
route URL/key or private config body is returned. IDs are scoped metadata, not
anonymous/public data. Callers privately retain the complete request before
submission. These values are concurrency conditions, not credentials, server
allocation proof or proof of human deletion intent.

The state digest binds exact canonical target messages, known context/observation/
fork receipt/terminal job rows, known table presence and FK-referenced IDs.
Each read set is capped at 5000 rows and the serialized state at 8 MiB. Existing
config limits still apply. History scans and FK discovery are not production
performance or legacy-data-quality acceptance.

Queued/running/stopping/unknown job status or upstream_unknown != 0 refuses.
Terminal, settled API jobs can be removed. Already-closed targets without a
matching coordinator receipt refuse; no assumption of a completed delete.
Malformed message JSON, ambiguous scope or unexpected known schema/views/triggers
fail closed. Message schema is the actual canonical six-column form.

## Transaction, closure and integrity

The buffered config callback checks metadata CAS, removes only the exact raw
target row, appends id/provider/deleted_at to the existing tombstone list and
preserves unrelated raw rows/extensions/routes/settings. It changes active only
when explicitly requested.

Within BEGIN IMMEDIATE, the candidate recomputes the entire expected state and
ID/redaction/retained-row packet before changing anything. It inserts the
api_web_closed_sessions marker (creating only this existing legacy table when
absent), removes scoped settled API jobs/context/observations/fork receipt and
deletes unreferenced message rows. Referenced message IDs are retained with
kind=deleted, text empty and fixed non-private deletion metadata, retaining
timestamp/direction to preserve existing relational integrity. The marker,
cleanup and pending coordinator intent commit together; SQL error/revocation or
process exit before commit rolls back all these effects and DDL.

FK inspection now recognizes case-insensitive references to messages and implicit
primary-key references such as REFERENCES Messages ON DELETE CASCADE. This small
shared-helper correction also applies to the legacy purge path. Referenced rows
are redacted so an implicit FK cannot cause an unexpected dependent-row cascade.

The real api_web_generation.Store.accept checks the durable closed marker.
Tests initialize that actual Store after a pending deletion and demonstrate it
cannot reserve another canonical input/job. This does not cover every ingress:
legacy/manual/unknown writers can bypass that Store and the cooperative gate.

## Recovery and historical receipts

Before publishing an unpublished pending config, recovery checks closure,
absence of known scoped messages/auxiliary data and exact retained redacted rows.
New bypass ingress, restored erased IDs, altered redacted rows, marker loss or
unsupported references refuse. It never repurges newly arrived/unreviewed data,
recalls a purge callback, silently abandons pending or reconstructs lost text.

After JSON is already the target hash, recovery only finalizes the historical
intent. Subsequent bypassed content is preserved, not swept up by a replay.
Committed same-request replay returns its original generation even after later
changes. Changed actor, timestamp, active selection, IDs or digests conflicts.
Different stale operations lose CAS; concurrent identical operations erase once.
No duplicate response asserts current content_deleted or global erasure.

Deletion is logically committed at the canonical intent transaction, before
JSON visibility. If authorization is later revoked, data already removed is not
restored; a blocked pending operation requires authorized reconciliation.
Missing/corrupt private stage or external config edits retain the existing
coordinator's explicit unknown-state behavior. No automatic compensation or
orphan-stage cleanup is introduced.

## Explicit limits

This is logical content cleanup through the supported canonical schema, not
forensic secure deletion. WAL/freelist/backups/caches and unknown stores may
retain data. Attachments/files, independent usage ledger, G3 source/event/revision,
other manually forked conversations and external/native stores are not erased.
Unknown denormalized references/copies and global source-revocation propagation
remain outside the proof. G3 deletion needs its own authorized audited protocol.

JSON and SQLite are not one atomic transaction. Ordinary readers may see the
old row after canonical cleanup but before tombstone publication; uncooperative
writers can act between checks and publication or after publication/finalize.
Known API Store acceptance is fenced; all-writer/runtime/source/version fences
are still incomplete. The returned WriteReceipt is historical configuration
publication evidence, not a complete privacy, live-window or UI success claim.

Keep production_supported=False, http_integrated=False,
all_writers_integrated=False, native_integrated=False, cross_store_atomic=False,
external_cleanup_supported=False and g3_delete_supported=False.
Raw coordinator SQL callbacks remain trusted; no hostile-caller/backup anti-
rollback proof is added. Production auth, packet persistence, attachments/MCP/
version/native/lifecycle deletion, HTTP, all writers and backup recovery remain
HOLD. ProjectionCard stays authority=derived; source/event/revision is truth;
commercial C is not a daily dependency. Frozen 36/36, 108/108 and historical
OpenAI 7/7 stay unchanged.

## Verification

34 new deletion tests + 388 prior focused regressions = **422/422 local PASS**
(18.087 s). Actual authority/delete entry, canonical messages DDL/connect,
context functions, FK declarations, API Store, coordinator journal, filesystem
publisher and independent processes are exercised with blocked sockets.

Coverage includes read-only/no-DDL snapshots and privacy; exact entry lock/path
and external-cleanup refusal; authorization before history and mid-effect
revocation; config/state/context/FK CAS; active replacement and last-API refusal;
native/legacy/attachment/MCP/version/lifecycle/ledger exclusions; job settlement,
known schema/triggers/JSON, byte cap and packet integrity; FK redaction/cascade
protection; transaction exit rollback, pending/after-publication exits, actual
Store admission rejection, pending damage/bypass refusal, historical replay,
exact packet/current authorization and concurrent processes. G3 fixture rows
and unrelated chat/config state are verified unchanged.

The local environment lacks FastAPI/httpx/starlette, so the separately fetched
legacy deletion HTTP test module cannot import locally and is not counted in
422. The repository's full Python CI with its installed test dependencies is
required to validate that existing module and all default routes. No synthetic
replacement is counted as an HTTP acceptance.

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation backend.tests.test_session_lifecycle_commands backend.tests.test_codex_canary_loop_integration backend.tests.test_web_session_provider_authority backend.tests.test_provider_model_migration backend.tests.test_provider_model_migration_request_contract backend.tests.test_session_lifecycle_model_commands backend.tests.test_session_lifecycle_session_lists backend.tests.test_session_lifecycle_api_creation backend.tests.test_web_session_fork backend.tests.test_session_lifecycle_api_fork backend.tests.test_session_lifecycle_api_delete -q
```

No merge, deployment, production DB/JSON/env/WAL/Journal change, restart, model
API, automatic memory/G3 write, live-chat creation/deletion or message send.
Production HOLD.
