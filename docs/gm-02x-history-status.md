# GM-02X: original-reference history status observation

The default-off internal HTTP installer adds POST /loop/lifecycle/history/status
with exactly request_id and packet_sha. References do not enter URL/query strings.
It uses the same header guard, server owner mapping, bounded strict JSON parser,
current command ACL and independent private packet ACL as prepare/execute. No
recover, method, principal, path or settings fields are accepted in a status body.

| Observed state | generation | Meaning |
|---|---|---|
| prepared | null | Valid retained original packet, no matching canonical intent at observation |
| pending | null | Matching original intent, canonical metadata and relevant config/stage hashes checked |
| committed | original base+1 | Matching historical committed receipt, not current config generation |

Every result is point-in-time observation, never a reservation, safe-to-retry
promise, current model-input assertion or guarantee future recovery will succeed.
In particular a prepared result does not authorize automatic replacement IDs;
pending does not assert all future recovery conditions or original stage schema
validation have passed. Committed history does not assert the requested setting
is still current. A later edit remains untouched.

Default-off inspect_history binds concrete PrivateHistorySetting and exact
PacketReference, validates original envelope/body and current server policy/
fallback digest exactly as the existing command identity. Policy/fallback changes
versus saved intent refuse observation rather than declaring unrelated success.
Routes/credentials/current setting/config/stage path are never returned.

Under the existing session lock and cooperative gate, a query-only canonical
transaction checks coordinator schema/resource binding, request identity and
metadata/config hashes. Prepared/committed observation refuses another pending
writer; pending observation is distinct even when JSON is already at new bytes.
Missing/corrupt relevant stage, external JSON changes, wrong reference or current
ACL loss refuse. ACL/private reference are rechecked before returning. The helper
does not call mutate/recover, create SQL intent, update journal/meta, publish JSON,
clean stages or reapply a mutation. Existing connection/gate bookkeeping can open
the lock file; this is not a promise of zero filesystem metadata activity.

Tests exercise actual FastAPI/ASGI and real temporary SQLite/JSON/private packets,
not TCP/TLS/proxy or deployed auth. They compare canonical content/generation/
journal/stage/packet snapshots across observations and simulate publication
before interrupted receipt commit. Existing 17 HTTP tests remain in regression.

No default startup invocation, client persistence UI/queue, automatic retry,
actual legacy writer replacement, native/runtime integration or production
activation is added. Original last-writer semantics and trusted-FS/fsync/size/
orphan-stage, named-path versus inode/antirollback, SQL/JSON and external ACL
limits remain. All-writer/read/ingress/runtime fencing and production backup/
recovery are still open. G3 truth/derived ProjectionCard/frozen baselines unchanged.
