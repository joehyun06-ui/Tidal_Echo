# GM-02AA: coordinated history snapshot and optional strict HTTP reads

The default-off history HTTP installer now exposes GET /loop/lifecycle/history.
It authenticates the existing internal header and server owner, accepts no body
or query selectors, and projects only history_n, generation and observed=true
from coordinator.observe_config. The injected session lock, cooperative gate,
query-only canonical transaction, resource/schema/config hash and guards before/
after projection are the existing coordinator contract. No private full config,
route/key, packet content or current model input is returned. The observer uses
read-only legacy config, not writable legacy methods.

This first bounded projection still requires current loop.update permission via
the original adapter admission; it does not grant a new read-only role or bypass
write authorization. Private packet storage ACL is not invoked because this
request reads no packet/reference. A missing, noninteger/bool or out-of-range
legacy count is unavailable, never coerced or defaulted to zero. Pending intent,
external config changes or final guard denial refuse the whole response. Reads
do not recover/publish or create canonical intent; generation is observation
metadata, not a reservation, original draft CAS or model consumption proof.

| Interlock installation | HTTP read admission |
|---|---|
| enabled=False (default) | Nothing installed |
| enabled=True, strict_reads=False (default) | Previous GET/HEAD/OPTIONS passthrough preserved |
| enabled=True, strict_reads=True | Only exact canonical GET history snapshot and existing three POST candidate paths pass |

Strict reads require that GET candidate already be installed. Other GET/HEAD/
OPTIONS routes authenticate then return 423 lifecycle_reader_blocked without
entering old handlers. This includes old config/sessions/provider reads, docs,
healthz and any readiness endpoint; no automatic production activation or claim
of normal-client compatibility is made. Middleware order, duplicate install and
pre-startup requirements stay. Default previous interlocked mode remains intact.
Query/body-bearing candidate reads are refused. Responses are private/no-store/
no-referrer; read failures return fixed read_unavailable rather than implying a
write outcome. Existing request conflicts/authorization status codes remain.

Tests cover bounded projection/no effects, actual committed count/generation,
pending read refusal, invalid legacy values, external JSON, final ACL denial,
authentication before reads, request shape and old/encoded read refusal. Full
API-loop/Uvicorn TCP tests verify old read rejection, candidate reads before/
after a coordinated write and unchanged non-strict behavior. No production code
startup, flags, CI or dependencies are changed; only candidate modules and the
isolated test worker gain explicit opt-in behavior.

This covers one coordinated projection plus HTTP admission in an isolated
service. Direct Python reads, background/manual/other-process access, native/
CLI/runtime, source/model/window consumption, client durable reference UI,
TLS/proxy/root_path and production backup/recovery remain open. Read snapshots
are point-in-time under cooperative locking, not atomic with external writers
or external ACL. Prior private-FS/fsync/size/orphan-stage, named-path versus inode/
antirollback and SQL/JSON limits, frozen baselines, G3 source truth/derived cards,
commercial C independence and production HOLD remain.
