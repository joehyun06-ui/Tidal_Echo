# GM-02Q private packets for empty API creation and plain API deletion

Default-off continuation of GM-02P. PrivateApiSessionCreate and
PrivateApiSessionDelete compose existing GM-02G/GM-02I commands with GM-02M
private packets. They install no HTTP routes, startup scans, credential issuer,
model dispatch, automatic recovery or native operation. Existing GM-02N/O/P
adapters and default legacy callers are unchanged.

## Actual transaction connection pin

SharedSessionCoordinator accepts optional canonical_path=None. With a supplied
path, _binding checks the actual PRAGMA database_list main path against an
absolute resolved expected path on each existing _meta/_binding validation,
including transactions/recovery/replay. A wrong, relative, aliased or redirected
connection refuses with a data-free resource-mismatch category. The existing
binding digest/schema/journal format and migration behavior are unchanged.
The None default preserves existing unpinned callers; no startup enablement or
database change is performed. A correct pin can open an existing installation
without replacing its metadata or bootstrapping another store.

The two new adapters require an explicit coordinator pin, not only a declared
filename. Their packet namespace is the existing digest of canonical DB/config/
gate paths, with exact concrete command/store/coordinator types and the same
injected session RLock. Plain deletion additionally requires its command's DB
path to match the pin. Actual transaction connection validation remains inside
the coordinator rather than relying on an earlier independent observation.
This is named-path binding, not inode attestation, prevention of same-path file
replacement, a signature, external ACL fencing or anti-rollback proof.

## Creation

prepare requires explicit stable session_id/request_id/title/UTC created_at and
activate bool. It validates current global/target write authority, the existing
fresh-row input shape, obtains the original list snapshot with its independent
metadata read scopes, and retains only explicit creation fields plus original
generation/snapshot SHA. It does not retain other session titles/routes, allocate
an ID/time, create a session, publish JSON or write canonical intent.

The metadata snapshot is not a target reservation or freshness guarantee;
existing target/tombstone/history/orphan/capacity checks are still performed by
the concrete create_api_session command at actual execution. State changes lose
the original CAS. execute accepts only target/reference/current access/recover,
checks strict packet identity/schema and invokes only that concrete command.
since_id=0, pinned=False and API provider remain fixed by the original command.
activate=False is retained; no implicit activation change occurs on replay.
Prepared-only recover cannot start creation. After intent/process exit, reopen
uses the exact retained packet and original staged JSON without a new snapshot,
timestamp, target ID or duplicate session append. Revocation refuses publication.
No inference is performed and no actual user chat is created by these tests.

## Plain deletion

prepare validates explicit request ID, UTC deleted_at, target/fallback scopes,
then retains the actual read-only erasure snapshot: original generation/config/
state digests, exact message IDs, redaction IDs and retained-row hash. It does
not close the session, erase rows, write intent or change active/config state.
execute loads the original packet and invokes only delete_conversation; the
concrete command independently rechecks current target/fallback authorization,
CAS and SQL closure/erasure proof. No attachment/native/ledger cleanup argument
or packet-supplied access/recover/method dispatch is accepted.

Prepared-only recover cannot start erasure. Pending recovery does not rerun the
purge or obtain another snapshot. New input, changed retained rows, lost closure
or current revocation refuses publication without deleting new data. After
JSON publication only finalization occurs, preserving later bypass input.
Referenced message IDs remain redacted as in GM-02I. This remains plain API
deletion: groups/versions/attachments/MCP/lifecycle/channel/native/known ledger
references and last-API/active-fallback restrictions remain unsupported/refused.

## Validation and remaining boundaries

Tests use contained real creation/delete fixtures, private files and actual
SQLite/JSON coordination. Shared assertions separately cover both commands;
they do not duplicate old TestCase discovery. Coverage includes no-effect
prepare, fixed fields/CAS, independent target/fallback/metadata read scopes,
current authorization and packet integrity, safe file modes/schema, default
off, explicit connection pins, wrong/relative pins, existing metadata
compatibility and redirecting a pinned coordinator to another independently
valid database. Actual child processes exit after creation/deletion intent and
recover original packets without duplication or repeated erasure. Child socket
connects are blocked. Revocation/new ingress and later-history preservation,
FK redaction and activate=False are exercised without real user/model data.

Private packets and SQL/JSON are not one atomic transaction. Retention reserves
nothing and cannot silently refresh stale preconditions. ConfigCommandAccess,
packet hashes and database pins are not credentials or human-intent proof.
GM-02M private storage/reference/256 KiB/uncertain fsync/orphan-stage/trusted
filesystem limits and GM-02G/I external-writer/runtime/logical-erasure limits
remain. No automatic cleanup, adoption, compensation, secure erasure or source
history rewriting is added. Metadata/config commands, publish_row, migration,
native and other adapters remain separate work, as do actual auth/HTTP,
all-writer/read/ingress/runtime fences and production backup/recovery.

Keep production_supported/http_integrated/automatic_recovery_supported False.
No merge/deploy/restart, production DB/config/env/WAL/Journal change, model API,
G3/automatic memory write or actual user-chat/message action. ProjectionCard
authority=derived, commercial C outside daily operations, frozen 36/36, 108/108
and historical OpenAI 7/7 unchanged. Production HOLD.
