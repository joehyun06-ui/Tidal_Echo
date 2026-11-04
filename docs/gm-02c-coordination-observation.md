# GM-02C cooperative writer and store observation candidate

This stacked, default-off candidate depends on GM-02B. It has no startup hook,
HTTP route, environment flag, production migration, worker, or model call.
`production_supported=False` and `all_writers_integrated=False` remain explicit.

## Cooperative JSON protocol

`SharedSessionCoordinator` takes explicit canonical-connection, JSON path,
private gate directory and validation callback dependencies. Construction does
no IO. `bootstrap()` is an explicit migration used only on disposable fixtures
in this milestone. The caller must provide trusted local POSIX storage, stable
paths and owner-only directories/files. No journal mode or WAL setting changes.

All participating **whole read/modify/write operations**, including session
patches and configuration updates, must use `mutate(request_id, parameters,
callback)`. Locking only `save_config` does not serialize the preceding read.
The callback receives a buffered legacy load/save interface and must be pure
apart from editing that buffer. Parameters must identify the full action,
target and payload, including schema/action version. This is a trusted internal
primitive, not an authenticated public mutation endpoint.

The protocol holds one per-process reentrant gate and POSIX advisory `flock`:

1. Read private JSON and verify the canonical journal binding/generation/hash.
2. Run the pure mutation, validate and write a private staged file with fsync.
3. Commit a pending SQLite intent containing digests and stage basename only.
4. Publish the JSON with the existing `atomic_write_text` helper.
5. Commit the new generation/config hash and committed receipt, then clean up.

The journal is in the canonical relay database under `session_coordination_*`;
it is separate from GM-02B's strictly owned `session_lifecycle_*` namespace.
Route secrets are not stored in journal rows. The temporary stage contains the
full configuration and requires the same privacy as the original JSON. A crash
before intent commit may leave an orphan stage; no automatic deletion or blind
cleanup of unexplained stages is implemented.

Pending intent blocks cooperative fences and new mutations. Explicit
`recover(request_id)` publishes a verified stage when JSON is still the old
hash, finalizes when JSON already matches the target hash, and refuses any
other hash. Missing/corrupt stage is refused while the old JSON remains. No
callback runs during recovery. Same committed ID/parameters returns its original
generation; different parameters conflict. Before durable intent exists, pure
computation may run again after a crash. After an exception, reconcile the same
ID rather than creating another operation. No automatic external-effect retry.

`SQLiteLifecycleRepository(..., coordinator=...)` holds the cooperative fence
across JSON capture, canonical transaction, commit, and final file observation.
Default construction preserves the earlier uncoordinated, disabled candidate.
Lock order is existing session lock -> shared gate -> canonical SQLite. Never
acquire the existing session lock from inside the shared gate. Nested read
fences use read transactions; nested config mutation is refused. Do not fork
while holding the gate. Lock-file replacement and uncooperative writes are
outside the cooperative guarantee; never delete or rotate the live lock file.

## Store observations are not runtime confirmation

`RuntimeStoreObserver` reads the existing selected-version binding and actual
API/Codex store tables, with query-only connections/transactions. The provided
read-only factory uses SQLite URI `mode=ro`; missing files are never created.
It does not initialize schemas, recover jobs, compact, dispatch or send content.

| Evidence | Observation | Remaining limitation |
| --- | --- | --- |
| API jobs and closed sessions in relay | Status counts, queued/running/stopping count, upstream-unknown count, availability | Quiet DB does not establish live stream or tool completion |
| Codex sessions/jobs in separate store | Session availability, thread presence, status counts, explicit dispatch-uncertain count | Independent point observation; no atomic relay/Codex snapshot |
| Optional Codex compaction table | Nonterminal request count | Missing table yields `None`, never zero/ready |
| Window/epoch, consumed source/message cursors, stream/tool registry, receipt | All unsupported (`None`) | A real runtime authority and confirmation protocol are still required |

Counts are scoped to the selected version, not a global worker-scheduler claim.
`explicit_uncertain_jobs=0` only means no explicit uncertainty marker was seen;
it is not proof of absence of upstream effects. No `SessionSnapshot` or
`WindowReceipt` is synthesized. `snapshot_complete`, `handoff_supported` and
`stores_atomic` always remain false. There is no lifecycle activation wiring.

## Validation and remaining gates

The two new suites cover concurrent processes preserving both RMW changes,
lock timeout/process death, crash before/after JSON publication, unavailable or
corrupt stage, external edits, validation/publish/SQL-finalization/cleanup
failure, idempotent restart, schema/privacy/binding refusal, repository-wide
fence, API active and terminal-unknown jobs, separate native job/compaction
observations, selected-version binding and read-only enforcement.

The combined regression command is:

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation -q
```

Remaining gates: inventory and wrap **every** actual configuration RMW writer
with common action identity/validation/auth and reviewed lock order; fence real
source/version/runtime authority changes; implement real window/epoch and
consumed-cursor observation/confirmation; validate durable production backup
and recovery; integrate real G3 source/auth. Advisory locks do not make
JSON/SQLite/Codex/runtime/G3 one ACID transaction, prevent whole-database rollback,
or constrain a writer bypassing the protocol. `cross_store_atomic=False` stays.

Production HOLD; no merge, deployment, DB/config/environment/WAL changes, live
model call, G3/automatic memory write, user-chat creation or message send.
Frozen 36/36, 108/108 and historical OpenAI 7/7 are unchanged.

References: [Python fcntl](https://docs.python.org/3/library/fcntl.html),
[SQLite transactions](https://www.sqlite.org/lang_transaction.html).
