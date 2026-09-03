# Hybrid Retrieval active promotion contract

Phase: **4D-D3C**

This document starts the reviewed promotion phase after a healthy Hybrid Retrieval
shadow canary.  A healthy shadow does **not** itself authorize provider-visible
Hybrid Memory.

## D3C1 — same-revision active selection and rendering foundation

D3C1 is intentionally unwired.  It defines the minimum authority contract a
future active runtime must satisfy:

1. Run the existing server-owned Hybrid query path.  Arbitrary client-supplied
   query vectors, sidecar paths, provider models, dimensions, or Memory keys are
   not accepted.
2. Require both BM25 and vector sidecar channels to be present and proved.  D3C1
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
   and the exact query text.  Any mismatch fails closed as stale.
7. Only then map the ranked Hybrid Memory keys back to plaintext from that fresh,
   proved snapshot.  Sensitive, non-global-user, inactive, unknown, or duplicate
   keys are never renderable.
8. Preserve rank order and stop at the existing provider-visible Memory budget:
   at most 10 items and at most 2000 content characters.  Never skip an oversized
   higher-ranked item to surface a lower-ranked one.
9. Reuse the existing `memory_context_developer_message/v1` JSON envelope and its
   instruction/data isolation policy.  D3C1 introduces no new prompt syntax.

D3C1 adds no environment variable, no relay/P3 import, no change to
`prepare_transient_memory_dispatch()`, no provider-visible authority, and no
production deployment action.

## D3C2 — future active runtime and default-off gate

D3C2 is a separate reviewed phase.  It must not be inferred from D3C1.
Before wiring provider-visible Hybrid retrieval, D3C2 must define and test:

- a dedicated repository-default-off active gate;
- shadow/active mutual-exclusion semantics;
- the exact point where Hybrid selection replaces the current authoritative
  selector while preserving the existing developer-message renderer;
- bounded request latency and cancellation behavior while query embedding is on
  the provider-visible critical path;
- deterministic behavior when Hybrid provider work, sidecars, or revision proof
  fail;
- whether failure falls back to the pre-existing authoritative selector or fails
  the generation request, with one explicit contract rather than ad-hoc catches;
- data-free active-path observability separate from `/readyz` unless a later
  review explicitly changes readiness semantics.

No D3C2 gate may be enabled merely because D3C1 tests pass.

## D3C3 — future production canary and rollback

Production activation requires another explicit deployment gate after D3C2 is
merged and deployed with the active gate OFF.  The canary must be serialized
first and must compare provider-visible behavior, latency, selected item counts,
and failure counters against the existing selector.

Rollback must be one bounded configuration action: set the active gate OFF and
redeploy.  Rollback must not change V1/V2 Memory truth, delete `relay.db`, delete
canonical Memory rows, or mutate disposable sidecars as a substitute for turning
the gate off.

## Permanent authority invariants

- Canonical messages and reviewed Atomic Memory remain truth; BM25/vector
  sidecars remain disposable derived projections.
- Hybrid ranking never becomes correction, forget, approval, or write authority.
- Sensitive Memory remains excluded from provider-visible Hybrid selection.
- Model/provider output never selects arbitrary Memory keys or rewrites official
  Memory.
- A revision mismatch is a hard stop for the Hybrid active path, never a reason
  to render stale plaintext.
