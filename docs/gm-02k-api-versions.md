# GM-02K recoverable composite API version creation

Default-off, non-production continuation of GM-02J. This implements an explicit
API edit/regenerate metadata-and-prefix operation. It never invokes inference,
resends a prompt, creates a live user chat, runs native Codex, or installs HTTP.

## One explicit composite entry

web_message_versions.create_api_version is distinct from the legacy
prepare/fork/finish sequence. It requires an injected command dependency, the
same session RLock, matching LOOP_CONFIG, declared relay_db and actual canonical
database path. There is no legacy fallback. Existing prepare/fork/finish/HTTP
callers remain unchanged and unintegrated; they must not be chained after the
new entry. Its result is a historical WriteReceipt, not created/UI/runtime state.

The root logical conversation remains fixed, active_session is preserved, and
the new underlying version row is attached to that root by a version edge.
Both edit and regenerate support existing nested API alternatives.
Native/mixed groups, invalid graph ancestry, unpublished members, closed/active/
unknown jobs and group MCP action references refuse through GM-02J validation.

The canonical UUID request_id fixes api-fork-<uuid hex>. Title, explicit UTC
created_at, root/source, operation point/mode and select flag must be retained
before submission. No new_row, UUID allocation or clock is invoked in mutation.
The stable packet is a trusted internal contract, not proof of server allocation,
credentials or human intent. Production transport/auth/private persistence is
still required before enablement.

## Preflight, authorization and source snapshot

snapshot_version is query-only and creates no tables, receipt or config change.
It returns generation, metadata/graph/source/copy digests and the existing sorted
group IDs, never message text, context settings, routes or private source bodies.
Global/root/source/new-target scopes and discovered group scopes are checked;
the original group plus new target remain authorized on write/replay/recovery.
Checks repeat in the gate, after copy SQL before intent and before publication.

GM-02J's group graph, known job/closed/tool validation and metadata CAS are reused.
The source digest binds the complete ordered source chat rows (not only the
copied prefix) and existing shared root-context record/table presence. Any source
append/edit or context change before intent requires another preflight.
Each source/copy payload is capped at 8 MiB and source history at 5000 messages;
existing 512-session config limits apply. Production scan/throughput and legacy
data quality have not been accepted.

Known target namespaces are checked at initial observation/intent and unpublished
recovery: messages/versions/forks/context plus API jobs/closure, lifecycle,
context observations, MCP, channel/native and known ledger references.
Unexpected known table schemas/views refuse. This is not a global reservation
across separate native/ledger or unknown/external stores.

## Prefix and annotation semantics

Edit requires an input point; regenerate requires an output point with a preceding
input. The copied prefix ends before that input for both modes. The selected
prompt is not copied or automatically sent. A first-prompt edit can create an
empty prefix. Prompt attachments refuse; earlier attachment references may be
retained, without accessing/copying files.

Stable point/prompt keys match the existing message_key rules: copied version
keys are preserved; the first fresh pair of an existing alternative reuses its
original point/prompt keys; later turns retain their own canonical IDs.
All copied rows are inserted already annotated with version_copy=True,
version_key and branch_origin. Historical version_usage is retained when present;
old generation_id/api dispatch metadata is omitted. No new generation or ledger
usage record is produced. Invalid/missing copied keys refuse.

Alternatives share the root's context. The candidate validates/binds root
settings at preflight/intent but never creates a target-owned context copy or
overwrites shared settings. Later settings changes remain shared, not replayed
from an old snapshot.

## Durable transaction and publication

The pure buffered config callback appends only the fixed new row, preserving all
prior raw rows/extensions/routes/settings and stored active. In BEGIN IMMEDIATE
the candidate recomputes graph/source/copy conditions and atomically commits:

- the new version edge;
- the fully annotated canonical prefix;
- a request fingerprint receipt in the existing web_session_forks table;
- the optional root selection (select=False preserves the previous stored row,
  including absence, and need not initialize a selection table);
- the coordinator pending intent for the private staged JSON.

Absent version/fork/selection tables are created only as needed in that same
transaction using existing definitions. No coordinator schema/format change,
parallel message DB or post-publication finish pass is introduced.
SQL failure, authorization loss before intent or process exit in the transaction
rolls all those effects and DDL back together.

Before publishing an unpublished intent, recovery verifies the exact copied
rows, edge, fingerprint, selection and lack of an unexpected target context/
known target namespace. New hidden ingress, damaged annotations, changed choice
or orphan actions refuse without deleting or overwriting them. Recovery uses the
original packet and committed copy; it never rereads changed source text to recopy.
There is no blind rollback of a committed copy or adoption of a legacy reservation.

After JSON already has the target hash, recovery only finalizes that intent.
It preserves later target history/context and newer selections. Committed replay
returns its historical generation without reapplying CAS or reconstructing the
version. Changed actor, allocation, select, body, group or digests conflicts.
Concurrent identical requests create once; another stale request loses CAS.

## Explicit gaps

SQLite effects and pending intent are atomic; SQLite and JSON/native/runtime
are not. The edge/selection can reference an unpublished target between the
intent commit and JSON publication. Cooperative preflight/mutations are blocked
by pending; ordinary/manual/unknown writers/readers may bypass that gate.
Root/source graph or settings can also change through bypass paths after intent;
the pending effect check does not reconstruct an atomic external source view.
Authorization loss or damage can leave a private copy and pending operation for
authorized reconciliation. Orphan-stage cleanup and automatic compensation are
not added. JSON publication does not confirm live context consumption or tool/
stream completion. Unknown states must not be reported as runtime success.

The broad version_creation_integrated flag stays False because production
HTTP/native/all-writer composition is not installed; the explicit in-process
API candidate is implemented here. Keep production_supported=False,
http_integrated=False, all_writers_integrated=False, native_integrated=False,
cross_store_atomic=False and runtime_selection_confirmed=False.
Native/version-group deletion, legacy repair/finish writers, source/runtime
fences, real window/epoch/consumed cursor, G3 source/auth and production
backup/recovery remain unfinished.

## Verification

36 new tests + 456 prior focused regressions = **492/492 local PASS**
(21.605 s). Real API prepare/fork/finish seeds existing alternatives before
bootstrap; the new composite entry, actual canonical DDL/connect, context,
selection, journal, filesystem and independent processes run with synthetic
data and blocked sockets. Local HTTP test dependencies remain absent; full
repository Python CI with its installed dependencies validates default routes
and legacy API/native paths, not a new HTTP installation.

Coverage includes no-write preflight, stable allocation, original config/source/
G3 preservation, atomic prefix/annotation/edge/selection/intent, empty prefixes,
nested/later-turn keys and historical usage, shared context/select=False,
exact entry lock/path, authorization/revocation, metadata/graph/source/context/
copy CAS, capacity/roles/attachments, half reservations/orphans, known jobs/
closed/tool/native refusal, target namespace checks, malformed copy keys/context,
transaction and optional-table DDL rollback, process exits before/after JSON,
effect damage/extra ingress, original-packet recovery, historical replay and
multiple processes. The created version is consumable by the GM-02J selector
without calling finish.

```sh
python -m unittest backend.tests.test_g3_projection backend.tests.test_g3_projection_feed backend.tests.test_session_lifecycle backend.tests.test_session_lifecycle_storage backend.tests.test_session_lifecycle_coordination backend.tests.test_session_lifecycle_observation backend.tests.test_session_lifecycle_commands backend.tests.test_codex_canary_loop_integration backend.tests.test_web_session_provider_authority backend.tests.test_provider_model_migration backend.tests.test_provider_model_migration_request_contract backend.tests.test_session_lifecycle_model_commands backend.tests.test_session_lifecycle_session_lists backend.tests.test_session_lifecycle_api_creation backend.tests.test_web_session_fork backend.tests.test_session_lifecycle_api_fork backend.tests.test_session_lifecycle_api_delete backend.tests.test_session_lifecycle_version_selection backend.tests.test_session_lifecycle_api_versions -q
```

ProjectionCard remains authority=derived; source/event/revision remains truth.
Commercial C is not a daily dependency. Frozen 36/36, 108/108 and historical
OpenAI 7/7 are unchanged. No merge/deploy/production DB/JSON/env/WAL/Journal
change, restart, model API, G3/automatic memory write, live chat creation/deletion
or message send. Production HOLD.
