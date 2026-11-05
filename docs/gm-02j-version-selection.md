# GM-02J atomic API version-selection metadata candidate

Default-off, non-production continuation of GM-02I. This milestone coordinates
existing API version selection; it does not create/copy/finish versions, run a
model, install HTTP, switch a live context or perform lifecycle handoff.

## Problem and explicit entry

The ordinary select_version path calls public_state, which may repair copied
history via finish, then writes selection outside the coordinator. That write
can invalidate lifecycle/version assumptions without changing config bytes.
The candidate bypasses those mutating helpers entirely and performs one SQL
selection with generation and a committed receipt in the canonical transaction.

web_message_versions.select_version accepts an explicit coordinated dependency,
the exact same injected session RLock, original request ID/access/packet and
resource paths. LOOP_CONFIG, declared relay_db and actual canonical DB must
match. Candidate metadata without the dependency refuses before legacy fallback.
Existing default calls and HTTP routes remain unchanged and unintegrated.
The explicit result is a historical WriteReceipt, not the old selected/UI state.

Only live API-authority groups are supported, including nested edit/regenerate
relationships with a valid finite parent chain to the supplied root.
A version cannot act as root; outsiders, unpublished/missing members, invalid
stored selection, mixed/native providers, cycles and missing parents refuse.
There is no silent selection fallback or graph/history repair.

## Read-only preflight and scoped packet

snapshot_selection reads private config plus a query-only canonical transaction.
It returns expected_generation, expected_snapshot_sha, expected_graph_sha and
the sorted expected_group_ids. No title, message text, settings, route key/URL,
source body or job payload is returned. Group identifiers are scoped private
metadata, not anonymous/public data. Missing optional tables are not initialized.

Global, root and requested target read/write scopes are checked first. Discovered
group identifiers are authorized before their history/config validation, then
the complete original group is retained in the packet and checked again for
write, replay and recovery. Each command uses a distinct read or select action.
Authorization is rechecked inside the gate and after SQL before commit.
Provider-history/ACL callbacks are still trusted external code, not an atomic
production identity service.

The graph digest binds the exact ordered group edges, stored selection including
absence, known API job rows, closed markers and relevant table presence. The
metadata digest and generation bind the existing config view. The packet binds
actor, action, root, requested target and all those preconditions. It is a
concurrency contract, not a credential, server signature or proof of human intent;
HTTP must still issue/bind/privately persist the full request before enablement.

Schema checks reject views, unexpected known columns and triggers on the version/
selection/job/closed tables. Groups are bounded by 512 configured sessions;
job reads stop at 5001 and reject more than 5000. Serialized graph state is capped
by the inherited 8 MiB hash helper. Known queued/running/stopping/unknown or
upstream_unknown jobs and closed group members refuse. Any group reference in
web_mcp_actions or web_mcp_codex_actions also refuses, even if it may be terminal;
this candidate does not interpret external-tool completion.

The snapshot inspects metadata only. It does not attest the correctness or
completeness of previously copied history, run finish annotations, authenticate
G3 sources or confirm consumed cursors. Published legacy alternatives may still
require their own creation/repair acceptance.

## Canonical-only coordinator transaction

SharedSessionCoordinator.mutate_canonical is a trusted SQL-only mode under the
same lock order: injected session RLock -> shared gate -> BEGIN IMMEDIATE.
It checks current config binding and expected generation, runs the effect with
a read-only legacy config, rechecks authorization/config bytes, and commits:

- the requested web_version_selections row;
- coordination generation + 1 with unchanged config SHA;
- the same journal schema's committed operation receipt.

The JSON config stays byte-identical, including raw extensions, routes, active
session and unrelated settings. No no-op JSON rewrite or artificial routing
marker is introduced. Old metadata saves are invalidated by generation even
when their config/metadata digest is otherwise identical.

An empty private 0600 stage file preserves the existing journal path/cleanup
format. It contains neither config nor a publication payload. There is no
pending JSON intent for this SQL-only mode. A crash before SQLite commit rolls
choice/generation/receipt and optional selection-table DDL back together.
A crash after commit but before cleanup/reply is handled by exact-ID replay or
explicit recover: return the historical receipt and remove the empty stage,
without executing selection SQL again. A missing receipt on recover never
creates a new operation. Fsync/commit outcome uncertainty stays explicit.

The effect callback is trusted, transaction-local SQL only: no manual commit,
external IO, native action or dispatch. Parameters must bind its full contract.
This is not a security boundary against a malicious raw coordinator callback,
tampered local database, filesystem/backup rollback or external operator.

Committed replay requires the original actor/target/group/packet and current
authorization for that original group. It returns the historical generation,
even after a newer valid selection, and does not reset that selection.
Different stale requests lose CAS; identical processes commit once.
The receipt proves committed coordination metadata, not that config JSON was
rewritten or a live model/window actually changed.

## Validation and open gates

34 new tests + 422 prior focused regressions = **456/456 local PASS**
(18.962 s). Tests seed real existing API alternatives through prepare/copy/finish
before coordinator bootstrap, then exercise the actual new select entry, real
canonical messages DDL/connect, coordinator journal, API job schema, filesystem
and independent processes with synthetic data and blocked sockets.

Coverage: read-only/no-schema/no-message-change snapshots, nested/root selection,
byte-identical config, atomic generation/receipt, exact entry lock/path,
global/root/target/sibling scope before their history checks, mid-effect
revocation, stale graph/metadata/generation, old bulk metadata-save rejection,
changed settled-job metadata, cycles/parents/unpublished/native/outsider/stored
selection errors, known jobs/closed/tool refusal, optional-table behavior,
schema corruption/triggers, malformed packets, fsync/external-file conflicts,
historical replay/current authorization, process exit before/after SQL commit,
transactional DDL rollback, empty-stage privacy and competing processes.

Local FastAPI/httpx/starlette are not installed. The repository's complete Python
CI with test dependencies validates existing default HTTP/native/version tests;
the focused count does not claim those local imports or new HTTP integration.

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation backend.tests.test_session_lifecycle_commands backend.tests.test_codex_canary_loop_integration backend.tests.test_web_session_provider_authority backend.tests.test_provider_model_migration backend.tests.test_provider_model_migration_request_contract backend.tests.test_session_lifecycle_model_commands backend.tests.test_session_lifecycle_session_lists backend.tests.test_session_lifecycle_api_creation backend.tests.test_web_session_fork backend.tests.test_session_lifecycle_api_fork backend.tests.test_session_lifecycle_api_delete backend.tests.test_session_lifecycle_version_selection -q
```

Known database job quietness is not runtime idle, stream/tool completion,
source/version fencing or confirmation of context_epoch/window/model/cursor.
Ordinary prepare/finish/public_state/delete/native and manual/default HTTP
writers can bypass the gate. Real auth/packet persistence, composite version
creation/repair/delete, native support, all-writer/ingress/runtime fences, G3
source/auth and production backup recovery remain unfinished.

Keep production_supported=False, http_integrated=False,
all_writers_integrated=False, native_integrated=False, cross_store_atomic=False,
version_creation_integrated=False and runtime_selection_confirmed=False.
SQL choice/receipt/generation are atomic together; no broader JSON/native/runtime
or source-authority atomicity is claimed. No coordinator journal DDL/format
migration or parallel message DB is introduced.

ProjectionCard remains authority=derived, source/event/revision is truth,
commercial C is not a daily dependency, and frozen 36/36, 108/108 and historical
OpenAI 7/7 remain unchanged. No merge/deploy/production DB/JSON/env/WAL/Journal
change, restart, model call, G3/automatic memory write, live user-chat creation/
deletion or message send. Production HOLD.
