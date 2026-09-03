# Hybrid Retrieval active promotion contract

Phase: **4D-D3C**

This document governs the reviewed promotion phase after a healthy Hybrid Retrieval
shadow canary. A healthy shadow does **not** itself authorize provider-visible
Hybrid Memory.

## D3C1 — same-revision active selection and rendering foundation

D3C1 defines the minimum authority contract the active runtime must satisfy:

1. Run the existing server-owned Hybrid query path. Arbitrary client-supplied
   query vectors, sidecar paths, provider models, dimensions, or Memory keys are
   not accepted.
2. Require both BM25 and vector sidecar channels to be present and proved. D3C1
   does not silently degrade provider-visible authority to a partial Hybrid
   configuration.
3. After the asynchronous query/embedding work completes, re-read the
   authoritative Atomic Memory snapshot.
4. Re-prove the BM25 sidecar against that fresh Atomic snapshot and require the
   exact BM25 sidecar generation used by the query result.
5. Re-prove the vector sidecar revision bindings against that fresh Atomic
   snapshot and require the exact vector sidecar generation used by the query
   result.
6. Recompute the local exact and lexical channel counts from the fresh snapshot
   and the exact query text. Any mismatch fails closed as stale.
7. Only then map ranked Hybrid Memory keys back to plaintext from that fresh,
   proved snapshot. Sensitive, non-global-user, inactive, unknown, or duplicate
   keys are never renderable.
8. Preserve rank order and stop at the existing provider-visible Memory budget:
   at most 10 items and at most 2000 content characters. Never skip an oversized
   higher-ranked item to surface a lower-ranked one.
9. Reuse the existing `memory_context_developer_message/v1` JSON envelope and its
   instruction/data isolation policy. D3C1 introduces no new provider prompt
   syntax.

## D3C2 — active runtime and repository-default-off gate

D3C2 wires D3C1 through the dedicated strict boolean gate:

`MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED=false`

The repository and Render Blueprint keep this gate OFF. CI rejects a different
committed default.

### Authority switch

The existing synchronous Memory preparation is executed by the relay in an
`asyncio.to_thread` worker. Network embedding is not allowed to run there because
client cancellation would not propagate. When active mode is enabled, D3C2 makes
one two-part server-owned switch:

1. the synchronous prepare callable validates the normal Memory-context request,
   does **not** run the legacy selector, and inserts one fixed internal developer
   sentinel immediately before the final user message;
2. the async Kelivo generator wrapper consumes exactly that sentinel on the
   request task, removes it, runs the D3C1 same-revision planner, and either
   inserts the existing Memory developer-message envelope or inserts no Memory
   message when Hybrid selects zero items.

The sentinel is never passed to the provider. Generator calls without that exact
server sentinel are delegated unchanged, so Telegram/other server-owned generator
paths are not implicitly promoted.

### Mutual exclusion

Hybrid active is a single provider-visible retrieval authority and therefore
requires all of the following:

- `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED=false`;
- `MEMORY_RETRIEVAL_V2_SHADOW_ENABLED=false`;
- `MEMORY_RETRIEVAL_V2_ACTIVE_ENABLED=false`;
- Memory core, context injection, and smart retrieval remain valid/enabled.

Active+shadow or active+V2 is a configuration error, not a precedence rule.
Installation fails before active callables are committed.

### Failure and latency contract

Hybrid active retrieval has a hard 60-second retrieval timeout. The timeout wraps
Hybrid retrieval/revision proof/render preparation only; normal model generation
keeps its existing independent timeout contract.

Client cancellation propagates through the active retrieval task into the
embedding/query path. A timeout, embedding/provider failure, stale sidecar,
revision mismatch, invalid render, or other active-path failure does **not** fall
back to the pre-existing selector. The generation request fails closed through
the existing `memory_context_unavailable` response path. This keeps authority
changes explicit and makes rollback a configuration action rather than a hidden
per-request mode switch.

D3C2 may lazily rebuild disposable sidecars through the existing D3B2 runner.
Cancellation/timeout can therefore leave a successfully completed disposable
sidecar refresh behind, but can never change canonical Memory truth.

### Data-free observability

Authenticated `GET /app/memory/hybrid-active/status` reports only bounded,
process-local structural state:

- enabled/installed/observability availability;
- in-flight and attempt counts;
- completed/failed/timed-out/cancelled outcomes;
- retrieval-only latest/max/total latency milliseconds;
- latest selected item count, character count, embedding-performed boolean, and
  a fixed data-free failure category.

It stores no query text, Memory key, Memory plaintext, vector, model, path, or
secret. Active status is deliberately separate from `/readyz`; a later review
would be required to make active retrieval health a readiness authority.

## D3C3 — future production canary and rollback

Production activation requires another explicit deployment gate after D3C2 is
merged and deployed with the active gate OFF. No D3C2 merge or gate-off deploy is
permission to enable active authority.

The D3C3 sequence must begin with a gate-off baseline, then disable Hybrid shadow
and enable Hybrid active in one reviewed configuration rollout. Initial active
canaries must be serialized and must check provider-visible behavior, retrieval
latency, selected item/character counts, embedding execution, and failure/
timeout/cancellation counters.

Rollback is one bounded configuration action: set the active gate OFF and
redeploy. Rollback must not change V1/V2 Memory truth, delete `relay.db`, delete
canonical Memory rows, or delete/modify disposable sidecars as a substitute for
turning the gate off. A later decision may separately re-enable Hybrid shadow,
but rollback of active authority does not require it.

## Permanent authority invariants

- Canonical messages and reviewed Atomic Memory remain truth; BM25/vector
  sidecars remain disposable derived projections.
- Hybrid ranking never becomes correction, forget, approval, or write authority.
- Sensitive Memory remains excluded from provider-visible Hybrid selection.
- Model/provider output never selects arbitrary Memory keys or rewrites official
  Memory.
- A revision mismatch is a hard stop for the Hybrid active path, never a reason
  to render stale plaintext.
- Repository defaults do not enable provider-visible Hybrid authority.
