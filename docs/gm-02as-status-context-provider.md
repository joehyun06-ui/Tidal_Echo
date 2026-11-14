# GM-02AS: request-scoped recovery context from durable epochs and canonical reads

Development/offline step after AR. Default off, production HOLD. No login,
credential issuer, new DB/schema, HTTP route, bootstrap, frontend or deployment
is installed. This does not complete real login or full GM-02 acceptance.

## Reused authority

`RecoveryStatusContextProvider` connects the existing durable AuthEpochRegistry,
SharedSessionCoordinator canonical read, WebSessionProviderAuthority, version
selection and checked lifecycle records to AR's TrustedStatusContext.

The only epoch-store extension is
`AuthEpochRegistry.assert_current_in_read_transaction(conn, epoch)`: it requires
an already-open query-only transaction on the same pinned main database, checks
the existing epoch row and leaves transaction lifetime to the caller. It does
no BEGIN/COMMIT, DDL or writes. Existing standalone assert_current and this method
share one row verifier qualified with main.memory_auth_epoch_v1, so a missing
main table cannot silently fall back to an attached database.

The provider requires coordinator.canonical_path and epoch_path to match. The
same SQLite read transaction observes the epoch and canonical state; the existing
cooperative gate fences private JSON/config and known writers. Both canonical
captures are query-only, and the provider never bootstraps, repairs, recovers or
issues an epoch. Existing epoch write APIs are used only in test setup/fault
injection, never in provider code.

## Host contract

| Input | Required source / behavior |
|---|---|
| enabled | Explicit true; default-off binding/authentication refuses before dependency IO |
| login_provider(request) | Real host authentication, expiry and credential checks, returning AuthenticatedRecoveryLogin(AuthenticatedLogin, ServerIssuedEpoch); sync/async allowed |
| authorize(login_identity, session_id) | Current server-owned canonical ACL; synchronous read-only, must authorize root and selected version separately and return matching AccessScope for that principal |
| historical_provider(session_id) | Existing trusted provider history; synchronous, not browser JSON |
| coordinator / epoch_path | Existing, explicitly initialized canonical resources; same pinned DB and existing private config/gate |
| identity_key | Exactly 32 server-only bytes, stable across restarts; no default/generated key and no env/config provisioning here |
| connection_namespace | Stable server-owned non-credential connection identifier, independent of request headers, token and API URL overrides |

Constructing AuthenticatedRecoveryLogin is not authentication. A legacy shared
Bearer check returning None, a review receipt, a dict, or a raw epoch string is
rejected; none is automatically promoted. Real credential verification and expiry
remain the login adapter's responsibility. AuthEpochRegistry itself has no TTL.
The existing MEMORY_TRUSTED_AUTH_EPOCH_ENABLED gate must already be enabled by an
authorized host; this provider does not set it or enable memory writing.

Canonical grant changes must revoke/rotate the durable login epoch even if the
final AccessScope later becomes equal again. A callback must re-read current
authority, not return a stale cached grant. This module supplies no ACL mutation
or login/logout service. An absent/unavailable/rotated/revoked epoch shares the
existing registry's denial category and maps to 403; other failed dependencies
map to a fixed 503. Operational recovery requires explicit host rebinding.

Authorization/history callbacks run under the existing coordinator gate and
query-only transaction: they must not write, initiate network/model work or take
the outer session writer lock. Existing lock ordering remains the host's duty.
The blocking canonical/epoch reads run with asyncio.to_thread and immutable
request-owned login captures; no global current-user slot is used. Cancellation
can discard the awaiter while a query-only worker finishes; it does not claim to
physically cancel every SQLite read or host callback.

## Sampling and labels

For each binding the provider authenticates and verifies the durable epoch,
captures canonical state, resamples the authenticated login, verifies epoch again,
and captures canonical state again. Login/epoch or snapshot differences deny the
binding. After the final snapshot closes, a fresh read checks the epoch again
to observe a logout or rotation committed while that WAL snapshot was open.
AR already calls authenticate before body, after body and after upstream
observation; its comparisons therefore use these real provider labels.

Canonical capture rechecks root/version grants under the gate, validates live
rows/tombstones/history and the root-selection relationship, and loads the checked
lifecycle record in read-only mode. Record AccessScope binding must match. Current
internal status journal is API-only: Codex records return unavailable rather than
claiming unsupported runtime verification.

| Label | HMAC inputs | Intended meaning |
|---|---|---|
| identity | Versioned purpose + connection namespace + trusted principal | Stable account/connection namespace for future AP local reference binding; not a credential or grant |
| auth_epoch | Separate purpose + namespace + principal/login + issued epoch + scope binding | Detects login/epoch/scope change without returning the raw server epoch |
| context_version | Separate purpose + namespace + root + coordinator generation + provider/selected version + full checked-record fingerprint + scope binding | Detects cooperative A→B→A and lifecycle-record changes; not current runtime/window/consumption evidence |

Labels are purpose-separated. Credentials are not used as the identity input.
Re-login retains identity and changes auth_epoch. Key/namespace changes create a
new identity namespace; old local records are not deleted, cancelled or granted
retry permission. The key needs an explicit durable provisioning/rotation plan
before real installation; this step does not create a production secret.

binding(request, conversation_id) returns a server-only RecoveryStatusBinding.
Only its identity is intended for a future separately authenticated capability
response. Its context stays server-side; no metadata endpoint is added here.
authenticate(request, conversation_id) directly implements AR's callback.

## Validation and limits

Synthetic login fixtures use real temporary canonical SQLite/private JSON,
durable epoch issue/rotate/revoke, coordinated generation changes, real session
authority/version checks and lifecycle decode/checksum checks. They include
same-byte canonical A→B→A, lifecycle revision changes, missing schemas/records,
owner/scope/version denials, credential rotation, distinct concurrent logins,
attached-DB refusal and no provider-side DB/config changes. New integration tests
run through AR gateway to the existing internal HTTP route and original token
guard; epoch rotation after upstream observation rejects the result.

Existing epoch review tests and the lifecycle suite remain applicable. New
canonical fixtures/coordinator are POSIX; this is not Windows provider acceptance.
No browser code changes: existing Chromium/WebKit recovery tests are regression
checks, not a browser-to-real-login/provider end-to-end test.

Cooperative generation is not an independent rollback/latestness bound, and
the two read transactions plus final response check are not a durable write grant
or atomic revocation lock through HTTP delivery. The upstream scope still must
be independently verified against AR's expected_access. Runtime/provider consumed
cursors, current-window confirmation, safe automatic switching and reply completion
remain separate work.

Next: a reviewed real-login adapter and server-owned capability/binding response
for the stable identity, then formal frontend/PWA wiring and runtime acceptance.
No merge/deploy, production DB/JSON/env/WAL/Journal change, real model API, G3 write
or actual chat. G3 source/event/revision remain authority, ProjectionCard derived;
commercial C is not a daily dependency. Frozen 36/36,108/108,OpenAI 7/7 unchanged.
