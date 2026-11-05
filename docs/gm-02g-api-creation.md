# GM-02G idempotent fresh API-session configuration publication

This default-off, non-production candidate builds on GM-02F. It covers explicit
creation/publication of a fresh, empty API session row. It is not lifecycle
reentry and must not create a replacement user chat for window handoff. No live
chat, model request, native session, HTTP route or deployment is performed here.

## Stable allocation packet and explicit scope

`CoordinatedApiSessionCommands` extends the existing metadata snapshot/authorization
interface. The caller must allocate and privately retain the exact session ID,
UTC ISO timestamp, stable request ID, title, activate flag, access principal and
base generation/snapshot digest before submitting. The candidate never calls
UUID allocation or `now_iso` inside the durable mutation. Changing an allocation
after an uncertain result cannot reuse the same operation ID.

The packet is a trusted internal contract, not proof that an ID was allocated
by the server. Real HTTP composition still must issue/bind/persist that packet
and authenticate the principal; client-provided fields are not credentials.
Missing ID/time is refused, rather than silently generating replacements.
Titles use the actual session authority normalization; fingerprints bind the
resulting canonical row, including the exact timestamp string.

Supported rows have exactly id/title/since_id/created_at/pinned/provider, with
provider=api, since_id=0, pinned=False and a valid explicit UTC timestamp. Native
Codex rows, history-backed rows, extra fork/native metadata and unsupported
fields refuse. The existing list capacity of 512 is retained. There is no env
fallback, auto request allocation, native qualification, pin, fork or dispatch.

Actions are `session.api.create.v1` and `session.api.publish.v1`. Each requires
current global and target-ID authorization. The target must not be a live JSON
row or tombstone, nor have observed provider history at initial mutation. The
append preserves all prior raw rows, extensions, tombstones, routes and unrelated
settings. `activate=False` preserves the active field; True points only to the
explicit new ID. This is ordinary manually requested session management, not
automatic continuity or model/window confirmation.

## Real Python entry bindings

Explicit optional dependencies are supported by:

- `examples.api_loop.create_session`;
- `WebSessionProviderAuthority.create_api_session` and `publish_row`;
- canary's installed legacy create wrapper;
- canary's async `create_web_session` API branch.

The entry's `LOOP_CONFIG` must match the candidate's bound path. Canary requires
the exact existing session RLock. Existing ordinary calls preserve behavior;
the authority/async entry's previously supported legacy session_id remains
available. Newly supplied candidate metadata without the dependency refuses.
Default HTTP callers do not supply this dependency and remain unintegrated.
Candidate results are historical `WriteReceipt`s, not the old created-row/UI
schema; the caller already owns the stable allocation packet.

An explicit candidate with provider=codex is rejected by the async wrapper
before controller qualification/pin/retirement. `publish_row` is only fresh API
publication, not a way to attest native creation or finalize a history fork.

## Canonical freshness checks and CAS

The fingerprint binds contract/action, principal, target, canonical row,
activation, expected generation and metadata snapshot digest. Generation is
checked before the callback and intent; the callback recomputes metadata digest
under the common gate. Old snapshots cannot append into a changed configuration.

The optional `require_fresh_session` precondition checks the existing canonical
database during BEGIN IMMEDIATE at initial observation and intent commit. It
refuses known references in:

- canonical message `meta.api_session`;
- message versions/selections, including parent references;
- API fork targets and session context roots;
- API jobs and closed-session markers;
- existing lifecycle records for that conversation.

Malformed message metadata (non-object JSON or non-text api_session), views or
missing required columns in a known namespace table fail closed. Absent optional
tables are not created. Identifiers are fixed; values are parameterized. No DDL
or new reservation table/index is added. The message check can scan history;
production performance and legacy data quality are not accepted by these tests.

This is a check of the listed canonical references and observed history, not a
global ID-reservation service. Separate native/usage-ledger stores, unknown
tables, manual writers, external source state and runtime admission are not one
transaction. Do not infer global uniqueness or all-writer fencing from it.

## Crash/replay semantics and the publication gap

Pending intent blocks cooperative mutations/preflight. Before publishing an
unpublished pending row, recovery rechecks canonical freshness. It must use the
original packet and current authorization; no new ID/time is generated. Already
published target JSON is only finalized, never appended or published twice.

After JSON becomes visible, ordinary API history might already exist for the
target if a legacy ingress bypasses the pending protocol. Finalization therefore
does not claim the target is still empty and does not erase that history. Tests
explicitly exercise this gap. This is **not** production-safe admission: live
ingress/worker/source/runtime fencing remains required before enabling the
composition. Unknown state stays explicit; there is no blind native retry or
whole-file compensation.

Same committed ID/parameters returns its historical applied generation even
after later title/history changes, with no new row. Different request IDs with
one stale base lose CAS; same-ID concurrent requests replay one receipt. A new
request cannot reuse an already-visible/closed/historical/tombstoned target.
Replay still requires current authorization and compatible provider identity;
it is not evidence of current emptiness, provider availability or window state.

The journal contains IDs, digests, generations and stage basename, not the title,
timestamp or complete row/route body. Private stages and retained allocation
packets contain private config/metadata and need GM-02C/F protections. This is
trusted local evidence, not anti-rollback signing or proof against a malicious
caller of the raw coordinator API. Optional preconditions must be bound into
that caller's complete parameter fingerprint.

## Verification

33 new tests + 312 existing focused regressions = **345/345 local PASS**.

Tests use the real API authority, canonical message DDL/connect, actual journal
and independent private JSON. Referenced namespace tables use synthetic fixtures
with the queried fields; actual version/context/fork DDL was checked in source.
API function tests isolate the complete real function through AST; existing
CI handles full default module/route regressions. No new HTTP acceptance is
claimed.

Coverage includes stable allocation and config preservation, actual create/
publish and sync/async wrapper entries, exact path/lock binding, missing
dependency, target scope and revocation, stale versions/digests, input/action/
actor conflicts, no UUID/clock calls, native rejection before controller calls,
orphan messages/versions/forks/context/jobs/closed sessions/lifecycle records,
namespace corruption and a write appearing before intent commit.

Actual subprocesses contend on the gate: different stale operations create
once, identical operations replay once, and process exits before/after JSON
publication recover exactly the same row. Pending recovery with new orphan
history refuses before publication; published recovery preserves legitimate
API history without asserting fresh emptiness. Messages are never generated
by the candidate. All test data is synthetic; sockets are blocked.

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation backend.tests.test_session_lifecycle_commands backend.tests.test_codex_canary_loop_integration backend.tests.test_web_session_provider_authority backend.tests.test_provider_model_migration backend.tests.test_provider_model_migration_request_contract backend.tests.test_session_lifecycle_model_commands backend.tests.test_session_lifecycle_session_lists backend.tests.test_session_lifecycle_api_creation -q
```

No merge, deployment, production DB/JSON/env/WAL/Journal change, restart, model
API, G3/automatic memory write, live user-chat creation or message send. Default
HTTP/legacy writers, native creation/retire, fork/delete, real auth and private
packet persistence, active ingress/source/version/runtime fencing, real window/
epoch/consumed-cursor/stream/tool confirmation, G3 source/auth and production
backup/recovery remain open. Keep `production_supported=False`,
`http_integrated=False`, `all_writers_integrated=False`, `native_integrated=False`,
`cross_store_atomic=False`; frozen 36/36, 108/108 and historical OpenAI 7/7 stay
unchanged. Production HOLD.
