# GM-02D explicit configuration command bindings

This is a stacked, default-off, non-production candidate on GM-02C. It binds
two config-only actions to real Python writer entry points. It does **not**
enable the protocol in HTTP routes, install a server, bootstrap production
tables, or establish that all writers cooperate. No deployment or merge.

## Audit scope and supported calls

At fixed parent `034a13a5f72d6059ff39c2c733d468df209ffc30`, all 222 tracked Python
source blobs outside paths containing `/tests/` were retrieved and scanned for
config/session writes and callers. The path/blob manifest and exclusions are in
`gm-02d-writer-inventory.json`. This static candidate scan is not a proof against
dynamic aliases, generated code, operator scripts or manual writes.

| Entry | Candidate behavior | Remaining boundary |
| --- | --- | --- |
| `examples/api_loop.patch_session` | Optional `coordinated`, request ID and access arguments delegate the whole patch | Existing HTTP callers omit these arguments |
| `examples/api_loop.update_config` | Optional coordinated branch; default branch shares the extracted pure update implementation | No automatic request-ID/auth translation or environment switch |
| `WebSessionProviderAuthority.patch_session` | Same optional command dependency; actual authority normalizes/validates buffered state | No bulk replacement or creation command |
| Canary's installed legacy `patch_session` | Uses existing session RLock and requires exact command-lock identity | No change to worker/native dispatch or async session creation |

`CoordinatedConfigCommands` is default disabled. Construction performs no IO.
Its only actions are `session.patch.v1` (existing title/pinned/active metadata)
and `loop.update.v1` (existing loop settings). Unsupported actions refuse before
authorization/dependencies, but this does not globally intercept their legacy
implementations. No monkeypatching of global config IO is installed.

The explicit Python branch returns a small `WriteReceipt`, not the public UI
response. HTTP route response schemas remain unchanged because the candidate
dependency is never supplied by those routes. A future opt-in HTTP composition
must translate errors/receipts and bind authenticated server identity and stable
request IDs before it can be considered integrated.

## Authorization, identity and recovery

The caller supplies an existing session lock, the exact historical-provider
reader, a pure server environment fallback-route reader, and a trusted
`authorize(access, action, target)` callback. There is no default grant. Only
literal `True` authorizes; missing access, false/truthy non-booleans and callback
failure refuse with fixed data-free categories. `ConfigCommandAccess` is not
a credential: the server callback must validate its principal against actual
trusted authentication. No production auth adapter is claimed here.

The digest binds contract/action version, principal, target, complete JSON
body, Render validation mode and a digest of server fallback routes. Input is
copied before mutation. A changed actor/action/target/body/policy/fallback route
cannot reuse the operation ID. Route secrets never appear in receipts or SQL
journal rows. The normal stage still contains private config and must retain
GM-02C's protections.

Authorization is checked before any config/fallback access, again inside the
common gate (including committed replay), after buffered mutation and before
publication. Recovery rechecks current authorization and compares expected
parameters to the persisted intent digest before publishing. It never reruns
the config callback. Permission loss after intent leaves explicit pending
reconciliation; it does not trigger an unauthorized publish or automatic undo.
Checks are authorization observations, not an atomic transaction with an
external ACL service; permission changes after the last check remain a future
production authority/fencing gate.

Lock order remains existing session lock -> shared gate -> canonical SQLite.
Canary's optional branch requires the exact same RLock object. The default
coordinator methods retain optional guards/expected-parameter checks, preserving
GM-02C callers that do not supply them. The low-level unguarded API remains a
trusted internal primitive, never a public recovery endpoint.

## Remaining writers

| Writer group | Why still unintegrated |
| --- | --- |
| `api_loop.save_config/save_sessions/create_session` and ordinary HTTP callers | Existing uncoordinated paths remain; stale bulk rows require a separate CAS/command contract |
| Authority `save_sessions/create_api_session/publish_row` | Publication and creation also have caller-owned state/identity boundaries |
| `web_session_delete._delete_conversation` (both JSON-save branches) | Message purge/redaction, upload cleanup, tombstones and native retirement form a composite operation |
| API/Codex forks and canary async native creation | Canonical version edges, source copying, native dispatch and JSON publication are not one transaction |
| `provider_model_migration.migrate_primary_provider_model/_restore_original` | Direct replace and best-effort restore bypass the shared gate; rollback must not overwrite a newer writer |
| Operator/manual writers | Advisory gate cannot constrain bypassing code or other processes |

Do not enable the repository fence in a live server and assume these paths are
covered. `all_writers_integrated=False`, `http_integrated=False`,
`production_supported=False` and `cross_store_atomic=False` remain.

## Runtime evidence audit

| Existing evidence | What it means | What it cannot prove |
| --- | --- | --- |
| `web_context_budget.CodexBudgetObserver.epoch` | A process UUID attached to compaction observations | Durable logical `context_epoch` or a new same-chat window |
| Native thread/turn and generation-job receipts | Native orchestration identity/status and canonical message binding | Exact G3 cursor consumed by that model window |
| API response usage / token totals | Reported provider usage on observed replies | Exact source/message cursor consumed or safe tool/stream boundary |
| `usage_ledger_continuation` source/target receipts | Canonical input/reply evidence for existing continuation creation | Same logical conversation window acceptance; must not be reused as lifecycle confirmation |

No window ID, context epoch, consumed cursor or confirmation receipt is inferred
from these values. GM-02C observations remain incomplete/unsupported for handoff.
Actual dispatch/input instrumentation and durable runtime confirmation are
still required; no automatic continuation is enabled.

## Verification

24 new command/pure-update tests plus the 184 prior lifecycle/projection tests
and 29 existing canary/provider-authority tests: **237/237 local PASS**.
An additional ad hoc comparison executed old and new entire update functions
over 48 combinations of config, payload and Render mode; results/errors/stored
state matched the fixed parent. It is compatibility evidence, not a new frozen
G3 benchmark.

New tests execute real WebSessionProviderAuthority and canary wrapper classes,
real JSON/SQLite coordination and isolated entire API entry functions extracted
from their AST. The AST approach avoids FastAPI startup/provider initialization
locally; it is not a full HTTP test. Existing repository CI covers full module
imports/HTTP behavior. Two actual API-entry subprocesses contend while one
holds the gate after reading/mutating its buffer, and preserve both settings and
session metadata; another subprocess exits after durable intent and recovers.
Authorization/replay/recovery conflicts and fixed error privacy are tested.

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation backend.tests.test_session_lifecycle_commands backend.tests.test_codex_canary_loop_integration backend.tests.test_web_session_provider_authority -q
```

Next: coordinate remaining composite writers and guarded model migration,
then opt-in authenticated HTTP composition; instrument real model input/window
consumption/stream/tool boundaries and confirmations. Production backup/restore
and real G3 source/auth acceptance remain separate gates. No production DB,
config, environment, WAL/Journal, restart, model API, G3 or automatic memory
write, new user chat or message send. Frozen 36/36, 108/108 and historical 7/7
stay unchanged; production HOLD.
