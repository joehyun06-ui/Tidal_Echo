# GM-02F versioned session metadata snapshots and bulk CAS merge

This stacked non-production candidate builds on GM-02E. It adds authorized
Python metadata preflight and an explicit coordinated `save_sessions` branch.
It does not install an HTTP route, startup hook, environment switch, schema or
production migration. Default constructors remain disabled and perform no IO.

## Supported operation and real entry bindings

`CoordinatedSessionListCommands.snapshot_sessions` returns a frozen
`SessionListSnapshot`: generation, metadata digest, immutable rows, and the
stored active-session value. `as_rows()` returns a detached mutable copy for
preparing a request. Each row contains exactly id/title/since_id/created_at/
pinned/provider. No route URL, key, row extension or raw config is returned.
Repr omits metadata. This is authorized metadata, not G3 source evidence or a
window/consumed-cursor confirmation.

`save_sessions` is explicitly bound to:

- `examples.api_loop.save_sessions` (isolated complete function tested locally);
- `WebSessionProviderAuthority.save_sessions` (real class);
- canary's installed legacy `save_sessions` wrapper (real class and exact lock).

These Python functions accept an optional coordinated dependency, server access,
request ID, expected generation and expected snapshot digest. Candidate metadata
without the dependency refuses rather than falling back to an uncoordinated
save. Existing ordinary calls preserve their behavior. No HTTP caller passes
the dependency; the candidate branch returns a `WriteReceipt`, not the old UI
response schema. Full HTTP/auth/request/response composition is still missing.

## Read snapshot and authorization

`SharedSessionCoordinator.observe_config` holds the cooperative gate and a
query-only canonical SQLite read transaction. Its trusted callback receives a
read-only buffered legacy interface and generation, not a writable API or
connection. It verifies ready state and unchanged observed config, creates no
intent and performs no config/schema write. The caller must still supply an
existing canonical connection factory; this is not a new DB/bootstrap API.

The session projection validates actual `WebSessionProviderAuthority` rows,
provider history, uniqueness and tombstones. It requires a materialized session
list of at most 512 rows. Global read permission and read permission for every
included ID are required and rechecked before return; partial authorized lists
are not silently presented as complete snapshots. The digest binds normalized
metadata and the presence/value of the stored active field. Missing active is
reported as None; blank remains blank. No latest-session fallback is inferred.
A nonblank stored active ID that is not in the list refuses preflight.

`ConfigCommandAccess` is not a credential. Trusted server authorization is
required, with literal True semantics and fixed errors. The read/write actions
are `session.metadata.read.v1` and `session.metadata.save.v1`. Write scope is
required for every submitted ID even if a row is unchanged. Provider history
is checked against requested immutable providers, including pending recovery.
These are checked observations; they are not an atomic transaction with an
external ACL or native-history service.

## CAS and metadata-only merge

The complete command fingerprint binds principal, action/contract, full rows,
active request, expected generation and expected snapshot digest. Generation is
checked in the coordinator before callback/intent and during recovery/commit;
the pure callback recomputes the metadata digest under the gate. Updating only
the expected generation on an old packet does not bypass the digest check.
Snapshots/digests are concurrency preconditions, not credentials, signatures or
proof that a human intended the submitted values. A caller must retain the
correct base snapshot when constructing an edit.

The submitted list must have the same IDs in the same order, with identical
provider/since_id/created_at. Only title and pinned may change, plus an explicit
active pointer to an existing listed ID. Add/remove/reorder/provider/history
identity changes refuse as unsupported topology operations. Unknown row keys,
wrong types, duplicates and oversized lists refuse. Existing authority title
normalization/validation is reused.

The candidate merges changed title/pinned into copies of the original raw rows,
preserving extensions, raw unchanged values, missing provider fields, route
secrets, tombstones and unrelated config. It does not wholesale replace rows
with their normalized projection. `active=None` preserves the stored field,
including absence; clearing/removing active is not part of this action. No new
conversation, version edge, message, native session or runtime window is made.

The same reviewed session lock must be used for related writers. Lock order
stays existing session lock -> common gate -> canonical SQLite. Canary rejects
a candidate dependency with a different RLock. The coordinator's read checks
now set query_only on their fresh connections; existing write transactions and
DDL remain unchanged.

## Recovery and replay

Pending intent blocks fresh snapshots and new writes. Explicit recovery needs
the original request packet (rows, active, base generation/digest, principal and
request ID); the caller must preserve that private packet. A fresh snapshot
cannot be used to guess an interrupted request. The journal stores only its
digest and stage metadata, not plaintext titles/rows. The stage still contains
private config and retains GM-02C permissions and handling requirements.

Recovery checks current authorization/provider observations and the exact
persisted input digest, then uses the existing old/target config hashes. It
does not rerun the metadata merge or infer a new topology. After publication,
it only finalizes the same intent. Source/provider changes or unrelated file
hashes refuse. Same committed ID returns its historical applied generation;
it never overwrites a later title/settings update and must not be shown as a
fresh current-state snapshot.

## Verification

33 new tests + 279 prior focused regressions = **312/312 local PASS**.

New tests cover frozen/redacted snapshots, detached row copies, query-only
connections/no writable buffer API, unchanged config/DB bytes and no intent on
read, per-ID scope revocation, history errors/changes, raw-extension preservation,
default and explicit real entry branches, canary lock identity, active semantics,
row limits/types/topology, stale generation/digest, replay identity, pending
recovery and external edits. Actual message rows remain unchanged.

Actual subprocess tests pause after buffered metadata mutation while holding
the gate. A reader waits, obtains the new generation and preserves both edits;
a writer carrying an old packet loses CAS without overwriting the winner.
Processes exit before/after publication and recover from the original packet
without repeating the merge/publication. All inputs are synthetic and sockets
are blocked. Local API-function tests isolate complete functions via AST; this
is not a new complete HTTP acceptance test. Existing repository CI covers
default module/route regressions.

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation backend.tests.test_session_lifecycle_commands backend.tests.test_codex_canary_loop_integration backend.tests.test_web_session_provider_authority backend.tests.test_provider_model_migration backend.tests.test_provider_model_migration_request_contract backend.tests.test_session_lifecycle_model_commands backend.tests.test_session_lifecycle_session_lists -q
```

## Remaining gates

This protects correctly bound explicit metadata-only calls, not every legacy
bulk replacement. Default HTTP/save/create/publish, fork/delete, native creation/
retirement and ordinary legacy model migration still bypass the candidate.
Topology changes and composite SQL/JSON/native commits need their own protocol;
do not route them through this metadata command. Manual/uncooperative writers
remain outside advisory-lock guarantees. Checksums do not prevent malicious
journal rewrite or whole-store rollback.

Real authenticated opt-in HTTP composition, all-writer inventory closure,
runtime active-work/source/version fencing, actual window/epoch/consumed cursor/
stream/tool confirmation, real G3 source/auth and durable production backup/
recovery remain open. Preserve `production_supported=False`,
`http_integrated=False`, `all_writers_integrated=False`, `cross_store_atomic=False`.
No merge, deploy, production DB/config/env/WAL/Journal change, restart, model
API, G3/automatic memory write, new user chat or message send. Frozen 36/36,
108/108 and historical OpenAI 7/7 remain unchanged; production HOLD.
