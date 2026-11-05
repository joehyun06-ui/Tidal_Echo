# GM-02E coordinated model migration and receipt-bound compensation

This stacked, non-production candidate builds on GM-02D. Constructors are
default off and perform no IO. No HTTP route, environment switch, startup
installation, schema migration, native/model dispatch or production activation
is added. Configuration commit is not evidence of runtime model availability,
window acceptance or consumed cursors.

## Existing entry and supported scope

`provider_model_migration.migrate_primary_provider_model` accepts an explicit
`coordinated` dependency, trusted access, request ID, expected generation and
expected old model. Without that dependency the existing legacy call and its
return schema stay unchanged. Supplying candidate request/recovery metadata
without the dependency now refuses, rather than falling back to a legacy write.
Ordinary HTTP callers do not supply the dependency and remain unintegrated.

The candidate requires an existing private JSON file containing exactly one
materialized primary route. `prepare_model_only_change` is a pure function:
validate the existing route, copy config, change only model, validate again.
It does not read environment routes or create missing config. URL, key, sessions
and unrelated settings remain unchanged. Legacy missing-file/env materialization
is not added to this protocol.

`CoordinatedModelCommands` requires an explicit canonical coordinator, existing
reviewed session lock and trusted authorization callback. The shared GM-02D
authorization function preserves literal-True semantics and fixed errors.
`ConfigCommandAccess` is not a credential. Production identity/authorization
and HTTP mapping are still missing. Lock order is existing session lock ->
common gate -> canonical SQLite. The caller must compose the relevant writers
with the reviewed lock; this module does not install or discover one.

## Compare-and-swap migration

`provider.model.migrate.v1` binds contract version, principal, primary-route
target, new model, expected old model and expected configuration generation.
The generation is checked inside the gate/SQLite transaction before the pure
callback and again before intent commit. The callback checks the observed old
model. A stale request cannot replace a newer cooperative change. Config-path
binding is checked lexically before config IO; the coordinator retains its
private-file/path protections. No schema changes are required.

After durable intent, the existing GM-02C pending/recovery protocol applies.
Recovery requires current authorization and the original complete parameters.
Old JSON plus verified stage completes publication; already-published target
only finalizes; any unrelated JSON hash refuses. No legacy `_restore_original`
call is made, and no model call or worker restart occurs. Accepted config
commands advance generation even when the requested model already matches;
the legacy same-model no-op behavior remains unchanged outside the candidate.

## Compensation is a new model-only command

`rollback_provider_model` accepts exactly:

```json
{
  "migration_request_id": "original-request",
  "migration_base_generation": 0,
  "previous_model": "old-model",
  "migrated_model": "new-model"
}
```

`provider.model.rollback.v1` requires its own request ID/current authorization.
It reconstructs the original migration fingerprint using the same principal,
old/new models and base generation. `CommittedOperationProof` checks the
canonical journal for that exact committed input digest, base generation and
published target hash. Current generation must be exactly original base + 1,
and current config hash must still match the original target. Missing, altered,
wrong-action or wrong-principal source receipts cannot authorize compensation.

The compensation builds a new config from the currently verified config,
changing only model back to the fingerprint-bound previous model. It never
restores a full old JSON file, decrements generation, erases a receipt, or
undoes runtime effects. Any later cooperative settings/model change causes a
CAS refusal, including changes unrelated to model. A fresh reviewed migration
is needed after such a change. A new operation cannot repeat the compensation
once generation has advanced; replaying its same ID only returns its historical
receipt.

Source proof and generation are checked at intent, pending recovery and final
commit. Loss of the source receipt blocks pending compensation recovery. A
published-but-unfinalized operation can remain pending; it is never silently
rolled back. Committed same-ID replay returns the historical applied generation
without mutation, even after later changes. It must not be displayed as the
current model state.

Journal rows contain hashes, generations, IDs and stage basename, not old config,
model text, route URL or key. Fingerprints/rows are trusted local evidence, not
signatures or protection against malicious journal rewrite/whole-database
rollback. The low-level coordinator remains a trusted internal primitive;
allowing untrusted callers to forge its parameters/callback is out of scope.

## Validation

28 new offline tests plus the 237 prior focused regressions and 14 existing
provider-migration/request-contract tests: **279/279 local PASS**.

Tests use the real migration entry, authority validation, atomic JSON helper,
canonical SQLite schema/connect, actual journal and independent private file.
They cover model-only preservation, no env/bootstrap/legacy-restore path,
strict candidate metadata, stale generation/model, same-ID replay after undo,
source fingerprint/actor/action conflicts, separate rollback authorization,
source loss during pending recovery, external edits and privacy.

Actual subprocess tests pause a model command after its buffered mutation while
the gate remains held. Concurrent settings preserve both changes and make an
old undo stale; two model commands using one base have one winner. Processes
exit directly before/after JSON publish; migration and compensation recover
without replaying callbacks or restoring an old full file. Sockets are blocked;
no provider/model request is made. Existing legacy migration tests still pass.

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation backend.tests.test_session_lifecycle_commands backend.tests.test_codex_canary_loop_integration backend.tests.test_web_session_provider_authority backend.tests.test_provider_model_migration backend.tests.test_provider_model_migration_request_contract backend.tests.test_session_lifecycle_model_commands -q
```

## Remaining gates

Default HTTP/legacy model migration still uses the existing uncoordinated
path, including its best-effort full-file restore. This candidate does not
make that path safe. All production writers must be bound/reviewed together
before enabling a live repository fence. Bulk save/create/publish, fork/delete,
native creation/retirement, operator/manual writers and their composite state
boundaries remain unintegrated. Advisory locks cannot constrain bypasses.

Opt-in HTTP composition still needs real auth, stable request identity,
CAS-read/preflight authority and error/receipt mapping. Runtime active work,
source/version changes and real window/consumed-cursor/stream/tool confirmation
are not fenced by these configuration checks. Real G3 source/auth and durable
production backup/restore remain separate gates. Keep
`production_supported=False`, `http_integrated=False`,
`all_writers_integrated=False` and `cross_store_atomic=False`.

No merge, deployment, production DB/config/environment/WAL/Journal change,
restart, model API, G3/automatic memory write, new user chat or message send.
Frozen 36/36, 108/108 and historical OpenAI 7/7 remain unchanged; production HOLD.
