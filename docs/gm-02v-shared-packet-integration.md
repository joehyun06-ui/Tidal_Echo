# GM-02V: shared private packet integration, offline only

The test-only bundle explicitly wires four existing adapters to one concrete
coordinator, canonical SQLite/JSON pair, injected RLock and private packet store.
It is not a production factory, generic dispatcher, HTTP route or runtime install.

| Path | Original command behavior retained |
|---|---|
| Single-session title/pin patch | Explicit last-writer field patch, no draft CAS |
| history_n setting | Explicit last-writer count, no draft CAS or message deletion |
| Model-only migration | Caller original generation and old-model CAS |
| Model rollback | Original committed source proof and immediate-generation CAS |

Real synthetic integration verifies cross-action request ID collisions in one
namespace, refusal of each reference by all other adapters, reopened current
authorization, no-effect preparation, metadata merge without lost updates,
concurrent model CAS with one winner, genuine rollback followed by metadata and
historical replay. A subprocess exits after migration intent but before JSON
publication; a different settings writer is blocked until explicit recovery of
the original model packet, which does not reapply the mutator. Later metadata
then invalidates the old rollback generation. Private packets cannot bypass the
shared pending-intent gate or grant undo permission.

Fixture socket connects/DNS are blocked; the crash child explicitly blocks
socket connect. No model service, actual chat, G3 write, native thread or HTTP
app is involved. Existing command/coordinator/packet implementations are not
changed. This proves these four opted-in paths under cooperative test wiring,
not all application writers, external/manual writes, process ingress or live
runtime behavior. Thread races do not stand in for multi-process ingress tests;
the real subprocess test proves crash/pending blocking, not all race schedules.

Existing defaults and HOLD remain: no automatic recovery, source/window/model
consumption guarantee, production backup certification, native integration or
auth/HTTP install. Trusted filesystem, 256KiB/fsync/orphan-stage, named-path not
inode/anti-rollback, external ACL non-atomicity and SQL/JSON limitations remain.
Global route editing and actual all-writer/read/ingress/runtime fences are open.
Frozen 36/36,108/108,historical OpenAI7/7 and derived ProjectionCard/G3 source
truth are unchanged. Commercial C is not a daily dependency.
