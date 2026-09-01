# Hybrid Retrieval shadow canary runbook

Phase: **4D-D3B4**

This runbook enables only the already-reviewed Hybrid Retrieval **shadow** path. It never changes provider-visible Memory authority. Existing V1/V2 retrieval remains authoritative throughout this procedure.

## Invariants

- `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED` is `false` in `render.yaml` and must remain false in repository configuration.
- Render Auto-Deploy remains disabled. Every deployment in this procedure is deliberate and manual.
- Do not change `MEMORY_RETRIEVAL_V2_SHADOW_ENABLED` or `MEMORY_RETRIEVAL_V2_ACTIVE_ENABLED` as part of this canary.
- Do not reuse the Memory fingerprint HMAC secret, relay/Telegram/Kelivo/Operit/audit/API-loop credentials, or any configured LLM API key as the Hybrid BM25 secret or embedding API key.
- Do not use the generation model as an embedding model merely because the provider is OpenAI-compatible. The configured endpoint/model must actually implement the `/embeddings` contract used by D3B2, including the requested dimensions.
- Hybrid sidecars are disposable projections under the persistent root. They are never Memory truth, correction authority, forget authority, or approval authority.
- `/readyz` deliberately does not gate on Hybrid shadow health. Canary health is read from the authenticated status endpoint.

## Required pre-existing Memory state

Before a Hybrid canary can be enabled, the deployed service must already have the existing Memory path healthy with:

- `MEMORY_CORE_ENABLED=true`
- `MEMORY_CONTEXT_INJECTION_ENABLED=true`
- `MEMORY_SMART_RETRIEVAL_ENABLED=true`
- a valid `MEMORY_FINGERPRINT_KEY_ID`
- a valid strong `MEMORY_FINGERPRINT_HMAC_SECRET` and matching pinned fingerprint profile

D3B1 fails closed if Core / Context Injection / Smart Retrieval are not enabled. D3B2 separately requires the fingerprint identity/secret because its authoritative Atomic snapshot reader re-proves active Memory rows before any sidecar rank can participate.

Do not enable or repair those preconditions as part of the Hybrid rollout itself. If they are not already healthy, stop the canary and handle the underlying Memory rollout as a separate gate.

## Required server-only Hybrid configuration

Populate these Render environment variables while the Hybrid gate is still `false`:

- `MEMORY_HYBRID_BM25_TERM_KEY_ID`
- `MEMORY_HYBRID_BM25_TERM_HMAC_SECRET`
- `MEMORY_HYBRID_EMBEDDING_API_BASE`
- `MEMORY_HYBRID_EMBEDDING_API_KEY`
- `MEMORY_HYBRID_EMBEDDING_MODEL`
- `MEMORY_HYBRID_EMBEDDING_DIMENSIONS`

The term key id is an identifier, not a secret, but it should change deliberately when the BM25 term secret is rotated. The embedding base must be HTTPS for a remote provider; plain HTTP is accepted only for loopback. Dimensions must be supported by the chosen embedding model and remain within the C3 contract bounds.

Never put real values in Git, issue/PR text, logs, screenshots, or the canary status endpoint.

## Stage 0 — code/deployment baseline

Before enabling the shadow:

1. Confirm the deployed branch is `feat/render-telegram-deployment` and includes the reviewed D3B3 observability merge plus this D3B4 configuration contract.
2. Confirm Render Auto-Deploy is OFF.
3. Confirm the pre-existing Memory state above is already healthy.
4. Confirm `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED=false` in the service environment.
5. Populate all six dedicated Hybrid settings above.
6. Perform one manual deployment with the gate still OFF.
7. Verify normal `/healthz`, `/readyz`, Telegram/Kelivo behavior, and current Memory context behavior are unchanged.
8. Query the authenticated `GET /app/memory/hybrid-shadow/status` endpoint. With the gate OFF it should report `enabled=false`, no in-flight shadow, and zero process-local counters.

This stage proves the rollout scaffolding itself is harmless. It does not validate the embedding provider because D3B2 deliberately does not read Hybrid provider credentials while the gate is OFF.

## Stage 1 — enable a serialized shadow canary

1. Change only `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED` to `true`.
2. Trigger one manual Render deployment.
3. Verify the service becomes healthy and `/readyz` retains its previous semantics.
4. Read the authenticated Hybrid status endpoint and require:
   - `enabled=true`
   - `installed=true`
   - `observability_available=true`
5. Send a small number of representative Memory-bearing requests **serially** at first. Include examples that exercise:
   - exact technical identifiers / environment-variable names,
   - ordinary lexical/CJK overlap,
   - paraphrased semantic recall.
6. After each request, wait for `in_flight=false` before sending the next initial canary request. This avoids confusing expected busy-drop behavior with provider or retrieval failure.

The first successful shadow may lazily build disposable BM25/vector sidecars. Formal generation does not wait for this shadow work.

## Stage 2 — structural acceptance checks

For an initial serialized canary, require all of the following before expanding traffic:

- `started > 0`
- `outcomes.completed > 0`
- `outcomes.failed == 0`
- `outcomes.cancelled == 0`
- `channels.bm25_available == outcomes.completed`
- `channels.vector_available == outcomes.completed`
- `outcomes.skipped.busy == 0` for the intentionally serialized sample

If the authoritative Atomic snapshot contains at least one eligible normal/global-user vector document, `channels.query_embedding_performed` should also track completed shadows. A zero-document eligible vector index may legitimately complete without sending the query to the embedding provider.

The relation histogram (`identical`, `reordered`, subset/superset, `mixed`) is **quality evidence**, not a safety pass/fail criterion. A mismatch does not change the provider-visible answer and must not be used to auto-promote Hybrid authority.

## Failure / rollback triggers

Disable the canary if any of these occur:

- startup fails because the dedicated Hybrid configuration is invalid;
- `outcomes.failed` increases during the controlled sample;
- the embedding provider behaves incompatibly with the bounded adapter;
- unexpected resource use or provider latency makes the single shadow slot persistently busy;
- any evidence appears that provider-visible Memory context changed because of the shadow path.

Rollback is intentionally simple:

1. Set `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED=false`.
2. Trigger one manual deployment.
3. Confirm the authenticated status endpoint reports `enabled=false` and no in-flight shadow.
4. Confirm normal `/readyz` and provider-visible Memory behavior remain healthy.

Do **not** change V1/V2 authority flags as part of rollback. Disposable Hybrid sidecar files may remain on disk; with the gate OFF they are unused. Never delete or replace the authoritative `relay.db` as a rollback action.

## Promotion boundary

A healthy shadow canary does not authorize active Hybrid retrieval. Promotion requires a separate reviewed phase with an explicit provider-visible authority contract, bounded context rendering, rollback semantics, and its own deployment gate.
