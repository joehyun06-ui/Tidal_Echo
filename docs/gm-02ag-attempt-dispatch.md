# GM-02AG: one-attempt trusted callback composition

Default-off attempt_dispatch_once requires a trusted synchronous plain function
adapter. It first records and validates through the existing fenced journal,
then invokes the adapter once with immutable attempt metadata, after commit and
postcommit validation, outside the SQLite transaction but inside the authority
fence. Historical replay does not invoke the adapter. Async/generator functions
and arbitrary callable objects are rejected before recording; returned deferred
objects are not executed and leave the outcome unknown.

No concrete API/Codex/native/provider adapter is installed. The callback must
actually honor the supplied request ID/digest; the composition cannot prove its
target, payload, provider idempotency or use of source evidence. It receives only
attempt metadata, not source text or an execution capability. Callback return
values are ignored: all observations remain unknown, dispatch_authorized=false,
window_confirmed=false. Existing activation is unchanged.

Adapter errors are normalized to lifecycle_dispatch_unknown after durable
recording, never retried. A process exit, lost commit ACK, failure between commit
and callback or postcommit validation failure may leave an unused attempt that
cannot be automatically retried. Availability is deliberately sacrificed under
uncertainty; reconcile the original request. There is no exactly-once delivery
or proof that any provider received or consumed a packet.

The authority must fence every relevant writer across the callback, without a
callback reentering incompatible locks or releasing the fence early. No timeout,
sandbox or async cancellation mechanism is supplied. Trusted code can bypass the
journal. Synthetic tests cover commit ordering/fence ownership, errors, replay,
lost ACK, deferred adapters and a fresh process exiting inside its fake adapter.
Production G3/runtime authority, live adapter/source consumption, API/Codex
independent acceptance, recovery UI and production backup/TLS remain open.

No startup/HTTP/env/schema/CI/dependency change. All tests use disposable data
and synthetic adapters; no real provider/model API. Default off, production HOLD,
frozen G3 baselines and commercial C independence unchanged.
