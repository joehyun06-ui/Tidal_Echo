# GM-02H recoverable explicit API branch-copy candidate

Default-off, non-production candidate on GM-02G. This is manual session
management, not lifecycle reentry or automatic window handoff. No live user
chat, inference, native service, HTTP route or deployment is invoked here.

## Problem and supported scope

The legacy API fork copies canonical history and context before publishing JSON,
then tries to delete the copy if publication fails. An uncertain filesystem
result makes that compensation unsafe; a retry can also allocate a new timestamp.
The explicit candidate instead commits the copy, context, fork receipt and
coordinator pending intent in **one canonical SQLite transaction**, then publishes
the private staged config through the existing recovery protocol. It never
compensates by deleting copied history.

Only mode=branch of an ordinary, unversioned, API-authority source is supported.
Edit/regenerate, native Codex and any source referenced by version edges or
selections refuse. Version relationship/selection publication and deletion are
separate unfinished protocols. The target is deterministically derived from the
canonical UUID request_id: api-fork-<uuid hex>. Title and explicit UTC created_at
must be supplied and retained before submission; no new_row/UUID/clock allocation
is used in the candidate. activate=False preserves the exact stored active field.

The public legacy fork function accepts an explicit coordinated dependency,
candidate packet and the exact same injected session RLock. Its config path must match LOOP_CONFIG, and relay_db must match
the declared canonical path. Actual PRAGMA database_list is checked against that
path on every fork observation/effect/recovery validation. Missing dependency
with candidate metadata refuses before legacy execution. Existing default calls
retain their legacy behavior and response; HTTP is not wired to the candidate.
The explicit branch returns a historical WriteReceipt, not the legacy created/UI
response. Its caller already owns the target ID and fixed allocation packet.

## Scoped preflight and stable packet

CoordinatedApiForkCommands uses the injected session RLock then shared gate.
Global, source-ID and target-ID authorization are required for separate fork read
and fork write actions. All those scopes are checked before historical-provider
dependencies; they are rechecked in the gate, after copy SQL before the intent,
and before publication/recovery. External ACL/history callbacks are still not an
atomic production identity service.

snapshot_fork reads a private config and query-only canonical transaction. It
returns four fields only: expected_generation, expected_snapshot_sha,
expected_source_sha and expected_copy_sha. It does not return message text,
context settings or route credentials, write an intent, create optional tables
or call a provider. These digests are concurrency/effect conditions, not
credentials or server-allocation proof. The real HTTP/auth layer must issue,
bind and privately persist the complete request packet before enablement.

The source digest binds the exact ordered raw messages at or before message_id,
including raw metadata, plus the existing source context record/table presence.
The requested point must actually be in that source's chat message prefix. Later
source messages are outside the selected prefix. The copy digest binds the
transformed messages and normalized copied context. Attachments retain existing
references; files are not copied. Prior generation/usage fields are omitted,
matching the legacy branch behavior. This operation creates no generated reply.

Existing 5000-message and 8 MiB serialized payload caps apply, along with the
512-session config cap. Message/history scans and full prefix reads are bounded
in row count but do not establish production performance or legacy-data quality.
Known namespace freshness from GM-02G is required before the copy; it is not a
global ID reservation across native, usage-ledger, unknown or external stores.

## Transaction and recovery semantics

The config callback checks generation/metadata digest and appends only the fixed
row, preserving all prior raw rows/extensions, routes, tombstones and settings.
Within BEGIN IMMEDIATE, canonical_effect recomputes source/copy digests, rejects
changed prefixes/settings and writes all copy effects with the pending intent.
The optional existing fork/context tables are structurally validated; views,
unexpected columns and triggers refuse. Absent fork/context tables are created
only inside this opt-in transaction using the actual legacy table definitions.
This changes no coordinator journal schema and creates no parallel message DB.

canonical_effect is a trusted internal SQL-only callback: no commit, external IO,
native action or dispatch. It requires a pending_validator and a real config
change. It runs only on first intent creation, never on committed replay or
recovery. Raw coordinator access remains trusted and is not a security boundary
against malicious callbacks, tampered backups or a caller bypassing the packet.

Before an unpublished pending config becomes visible, pending_validator checks
the exact target copied rows/context and fork receipt against the original copy
digest/fingerprint. Damage, unexpected hidden target ingress or missing effects
refuse without deletion. Recovery uses the retained allocation/packet and current
authorization; it does not recopy the source if the source changes after intent.
Revocation can leave a blocked pending intent and retained private copy requiring
authorized reconciliation; no automatic compensation or garbage collection is
claimed. Orphan private stage files before a committed intent remain a separate
operator/recovery concern inherited from the coordinator.

After JSON is already the target hash, recovery only finalizes the original
intent. It does not overwrite later target history or context edits, recopy a
prefix, or assert that the branch is currently unchanged. A committed replay
returns its historical generation even after source/target/config changes;
changed actor, request payload, allocation or expected digests conflicts.
Same-request processes create one copy; another stale request loses CAS.

**Cross-store atomicity is not established.** Legacy readers/writers can bypass
the cooperative gate and see hidden canonical rows, act between validation and
file publication, or write after publication before finalization. Tests expose
pre-publication hidden ingress refusal and preserve legitimate post-publication
activity. This is not proof of live ingress admission, runtime/source/version
fencing, global ID reservation, source-revocation propagation or UI success.
No automatic abort, deletion receipt, rollback transaction across JSON/native
state, production backup restore or exactly-once runtime dispatch is claimed.

## Verification

36 new tests + 345 prior focused regressions + 7 legacy API-fork tests =
**388/388 local PASS**. Real authority, fork entry, canonical message DDL/connect,
context functions, coordinator journal, filesystem writer and independent
processes are exercised with synthetic data and blocked sockets. Version schema
fixtures remain minimal for explicit refusal; no native/HTTP acceptance is claimed.

Coverage includes query-only/no-DDL preflight, fixed allocation, preservation of
raw config/source rows and active pointer, attachment references and usage
omission, read/write scope checks before history access, revocation during copy,
database/path mismatch, changed metadata/source/context/copy digest, late source
change before intent, native/version refusal, namespace/schema/corruption,
history caps and journal privacy. SQL failure and process exit inside the copy
roll back DDL/rows/receipt/intent together. Exits before/after JSON publication,
pending effect damage, exact-packet recovery, historical replay and competing
processes exercise the concrete durable protocol.

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation backend.tests.test_session_lifecycle_commands backend.tests.test_codex_canary_loop_integration backend.tests.test_web_session_provider_authority backend.tests.test_provider_model_migration backend.tests.test_provider_model_migration_request_contract backend.tests.test_session_lifecycle_model_commands backend.tests.test_session_lifecycle_session_lists backend.tests.test_session_lifecycle_api_creation backend.tests.test_web_session_fork backend.tests.test_session_lifecycle_api_fork -q
```

Keep production_supported=False, http_integrated=False,
all_writers_integrated=False, native_integrated=False and cross_store_atomic=False.
Remaining gates include HTTP/auth/private packet transport, native and version
topology, delete/retire, ordinary writers, active ingress/runtime/source/version
fences, actual window/epoch/consumed cursor/stream/tool confirmation, G3 source/auth
and production backup/recovery. ProjectionCard remains authority=derived and
source/event/revision remains authoritative. Commercial C is not a daily
dependency. Frozen 36/36, 108/108 and historical OpenAI 7/7 stay unchanged.

No merge, deployment, production DB/JSON/env/WAL/Journal change, restart, model
API, G3/automatic memory write, live user-chat creation or message send.
Production HOLD.
