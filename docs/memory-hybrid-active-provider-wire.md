# Hybrid Active Provider-Wire Canary Contract (D3C2.1)

This document narrows the provider-visible rollout contract after the first
D3C3 Active canary was rolled back on 2026-09-03.

## Incident evidence

The first serialized Active request returned HTTP 502 to the Kelivo client. The
preserved response body contained only the bounded category
`provider_explicit_rejection`. Production was immediately rolled back by setting
`MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED=false` and redeploying. No canonical
Memory row, sidecar, V1/V2 gate, or Hybrid shadow gate was changed as part of the
rollback.

That category proves the request reached the downstream generation provider and
received an explicit non-transient rejection. It does **not** prove that Hybrid
selection or Memory rendering caused the rejection. The previous rollout did
not run an immediately-adjacent gate-OFF control request, so provider health and
Active-specific wire behavior were not isolated.

## D3C2.1 provider-wire lifecycle

`backend.memory_retrieval_hybrid_provider_wire` is installed only after the
reviewed D3C2 Active runtime. It changes process-local observability semantics
only:

- Hybrid selection/rendering remains owned by D3C1/D3C2.
- Provider messages, Memory plaintext, query text, vectors, model configuration,
  canonical Memory, and sidecars are not modified by D3C2.1.
- D3C2's `record_completed()` call is deferred task-locally while the real
  provider generation is still in flight.
- `outcomes.completed` increments only after the downstream generator returns
  successfully.
- If downstream generation fails after Hybrid retrieval completed,
  `outcomes.failed` increments, `outcomes.completed` remains unchanged, and the
  data-free provider failure category is retained in `last.failure_category`.
- Provider cancellation after retrieval is counted as `cancelled`, not
  `completed`.
- A bounded data-free log line is emitted at provider completion/failure so the
  evidence survives a fail-closed rollback and process restart.
- Non-Active generator calls are delegated with the exact original arguments
  and do not affect Active counters.

The provider-wire patch is a no-op when the Active gate is false.

## Required paired-control rollout

Do not enable Active directly from a zero-counter baseline. Every Active canary
must have an immediately-adjacent gate-OFF control using the exact same user
query.

### Stage 0 — gate-OFF baseline

Require all of the following before any provider request:

- `MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED=false`
- `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED=false`
- Memory Retrieval V2 shadow/active remain false
- `/app/memory/hybrid-active/status` returns HTTP 200
- `enabled=false`, `installed=true`, `attempts=0`, `in_flight=0`
- Active outcomes are all zero on the fresh process

### Stage 1 — paired control request (Active still OFF)

Send one representative Kelivo request with a unique idempotency key while
Active remains false. Record the exact user query text locally for the next
stage.

Pass only if:

- Kelivo returns HTTP 200;
- the normal provider-visible Memory path completes;
- Active status remains `attempts=0` because the gate is still OFF.

If this control is not HTTP 200, **stop**. Do not enable Active. Investigate the
provider/current normal Memory path independently.

### Stage 2 — enable Active, no canary yet

Change only:

`MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED=true`

Keep Hybrid shadow and both V2 gates false. Deploy and require:

- service live/healthy;
- Active status HTTP 200;
- `enabled=true`, `installed=true`, `observability_available=true`;
- fresh-process `attempts=0`, `in_flight=0`, all outcomes zero.

Do not send a canary until this status baseline is captured.

### Stage 3 — paired Active request

Send exactly one Kelivo request using the **same user query text** from Stage 1
and a new unique idempotency key. Do not send a second request until status and
logs are inspected.

Pass only if all of the following hold:

- Kelivo returns HTTP 200;
- Active `attempts=1`;
- `completed=1`;
- `failed=0`;
- `timed_out=0`;
- `cancelled=0`;
- `in_flight=0`;
- `last.status=completed`;
- if eligible vector documents exist, `last.query_embedding_performed=true`.

The provider-wire log must contain one bounded
`status=completed stage=provider` receipt for the request.

## Fail-closed rule

Any non-200 Active canary, Active `failed>0`, `timed_out>0`, unexpected
cancellation, or provider-wire failure receipt fails the rollout.

Immediately:

1. set `MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED=false`;
2. deploy;
3. verify `/app/memory/hybrid-active/status` reports `enabled=false` on the new
   process;
4. do not retry Active until a reviewed fix is merged and the paired-control
   sequence starts again from Stage 0.

Do not delete canonical Memory, BM25/vector sidecars, `relay.db`, or change V1/V2
or Hybrid shadow gates as part of this rollback.

## Interpretation

The paired control is the authority for causality:

- gate-OFF control fails -> provider/normal path problem; Active is not tested;
- gate-OFF control passes and identical Active query fails -> Active/provider-wire
  difference is proven and must be investigated;
- both pass -> provider-visible Active canary is structurally healthy for that
  sample only; this does not authorize broader rollout without its own gate.
