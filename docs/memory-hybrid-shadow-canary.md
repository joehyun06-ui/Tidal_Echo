# Hybrid Retrieval shadow canary runbook

Coverage: legacy query-repair shadow (**4D-D3B4**) and worker-backed shadow (**4D-C5/C6**).

This runbook describes two alternative Hybrid Retrieval **shadow** rollout paths. It never changes provider-visible Memory authority. Existing V1/V2 retrieval remains authoritative throughout either procedure. A runbook, green CI, or a merged PR does not authorize a deployment, a production gate change, or a live smoke; each requires its own explicit approval.

## Invariants

- `MEMORY_INDEX_REFRESH_WORKER_ENABLED`, `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED`, and `MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED` remain `false` in repository configuration. Hybrid Active stays OFF for this entire runbook.
- Render Auto-Deploy remains disabled. Every deployment in this procedure is deliberate and manual.
- Do not change `MEMORY_RETRIEVAL_V2_SHADOW_ENABLED` or `MEMORY_RETRIEVAL_V2_ACTIVE_ENABLED` as part of this canary.
- Do not reuse the Memory fingerprint HMAC secret, relay/Telegram/Kelivo/Operit/audit/API-loop credentials, or any configured LLM API key as the Hybrid BM25 secret or embedding API key.
- Do not use the generation model as an embedding model merely because the provider is OpenAI-compatible. The configured endpoint/model must actually implement the `/embeddings` contract used by D3B2, including the requested dimensions.
- Hybrid sidecars are disposable projections under the persistent root. They are never Memory truth, correction authority, forget authority, or approval authority.
- `/readyz` deliberately does not gate on Hybrid worker/shadow health. The authenticated status endpoints provide process-local evidence for a separate canary decision, not readiness or index-freshness proofs.

## Startup modes and writer ownership

In this table, Worker means `MEMORY_INDEX_REFRESH_WORKER_ENABLED` and Shadow means `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED`. Hybrid Active must be OFF.

| Worker | Shadow | Status `mode` | Index writer / shadow behavior |
| --- | --- | --- | --- |
| OFF | OFF | `disabled` | Neither worker nor shadow writes or queries Hybrid sidecars. |
| ON | OFF | `worker_only` | Worker reconciles sidecars and acknowledges the observed outbox prefix; no query shadow. |
| ON | ON | `worker_readonly_shadow` | Worker owns refresh; an independent shadow runner queries with read-only DB connections and never repairs indexes. |
| OFF | ON | `legacy_query_repair` | Legacy shadow queries can lazily build or repair sidecars. |

These are **startup** modes. A gate change requires a separately approved restart/manual deployment; editing the environment does not reconfigure an already-installed worker or shadow. Worker + Hybrid Active is rejected by startup validation. This runbook's status contract reports Active ON or inconsistent/missing markers as `mode=unavailable`, never as proof that there is no index writer.

Two partial rollbacks have different effects:

- Shadow OFF alone leaves the worker running and writing indexes.
- Worker OFF alone, with Shadow still ON, restores the **legacy query writer** after restart. It is not a no-write rollback.

To stop index work from this rollout, set **Worker OFF + Shadow OFF**, keep Hybrid Active OFF, and perform the approved restart. Do not alter V1/V2 retrieval or Memory authority flags.

## Read-only status contracts

`GET /app/memory/index-refresh/status` uses the existing relay authentication. It reports installed mode markers, task liveness, and bounded process-local worker counters. It does **not** open any database, inspect paths, reread provider/configuration settings, call an embedding provider, refresh an index, read or acknowledge the outbox, or touch Memory authority. There is no POST/action endpoint.

| Field | Meaning / limitation |
| --- | --- |
| `contract_version` | `memory-index-refresh-observability-v1` |
| `enabled`, `installed`, `shadow_enabled`, `active_enabled`, `mode` | Installed process state, not live environment values. `installed=true` alone does not mean a worker is enabled or running. |
| `observability_available` | Marker/counter data is structurally available. It does not mean the worker is healthy, indexes are current, or the outbox is empty. Missing/corrupt telemetry returns false without changing worker behavior. |
| `task_state`, `task_running` | `disabled`, `not_running`, `running`, `done`, `cancelled`, or `unavailable`. A running task is only a liveness observation. |
| `in_flight` | A drain attempt is being observed. Polling and failure backoff are not in-flight attempts. |
| `attempts`, `outcomes` | Counts of attempts and idle/completed/failed/cancelled attempts in this process. Idle is not a completed reconciliation. Cancelling polling/backoff is not an additional cancelled drain. |
| `consecutive_failures`, `backoff_seconds`, `last` | Failure streak, last scheduled delay (not a countdown), and a fixed status/category. No raw exception text is exposed. |
| `last_completed_receipt` | The last drain that returned after reconciliation **and**, when there was a batch, acknowledgement. Retained across later idle/failure/cancellation; null until a completion is observed. |

Inside `last_completed_receipt`, `batch_pending_count` is the **historical selected batch size**, and `batch_completed_count` is the completion receipt for that batch. Neither is current backlog. `source_atomic_count`, BM25/vector document counts, `provider_call_count`, and `rebuilt` describe that same historical reconciliation. A rebuild followed by failed acknowledgement increments `outcomes.failed` and does not replace this receipt. `provider_call_count` is not a lifetime/provider-billing total and excludes failed attempts.

Counters saturate at 1,000,000 and reset with a new process. In multi-process/restart observations, never add or subtract uncorrelated snapshots as though they were a durable ledger. No Memory content, item keys, queries, model identities, paths, secrets, vectors, or provider response bodies are returned. If `observability_available=false`, stop acceptance and investigate separately; do not interpret zero fallback counters as success.

`GET /app/memory/hybrid-shadow/status` keeps its existing response contract. Use it separately for shadow outcomes/channels. Neither endpoint changes `/readyz` or the provider-visible retrieval dispatch, and neither automatically enables Hybrid Active.

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

Populate these Render environment variables only with separate configuration approval, while Worker, Shadow, and Hybrid Active are all `false`:

- `MEMORY_HYBRID_BM25_TERM_KEY_ID`
- `MEMORY_HYBRID_BM25_TERM_HMAC_SECRET`
- `MEMORY_HYBRID_EMBEDDING_API_BASE`
- `MEMORY_HYBRID_EMBEDDING_API_KEY`
- `MEMORY_HYBRID_EMBEDDING_MODEL`
- `MEMORY_HYBRID_EMBEDDING_DIMENSIONS`

The term key id is an identifier, not a secret, but it should change deliberately when the BM25 term secret is rotated. The embedding base must be HTTPS for a remote provider; plain HTTP is accepted only for loopback. Dimensions must be supported by the chosen embedding model and remain within the C3 contract bounds.

Never put real values in Git, issue/PR text, logs, screenshots, or the canary status endpoint.

## Worker-backed path — baseline, worker-only, then read-only shadow

Use this path only after the reviewed C5/C6 code **and the index-refresh status route** are confirmed in the deployed commit. A merged source branch is not evidence of a deployment. Do not execute the legacy stages below as part of this path.

1. **Baseline, all three gates OFF.** Confirm the target branch is `feat/render-telegram-deployment`, the exact deployed commit contains the reviewed changes, Render Auto-Deploy is OFF, dedicated settings are valid, and the existing Memory preconditions are already healthy. After the separately approved manual deployment, check normal service/Memory behavior. Require the new endpoint to report `mode=disabled`, `enabled=false`, `task_state=disabled`, and `observability_available=true`. The old shadow endpoint must also remain disabled. Gate-OFF startup/status does not validate the embedding provider.
2. **Worker-only, separate gate/deployment.** Enable only Worker. Require `mode=worker_only`, `installed=true`, `enabled=true`, `task_running=true`, and `observability_available=true`. Within an agreed bounded observation window, require `outcomes.completed > 0`, a non-null `last_completed_receipt`, `consecutive_failures=0`, and no failed/cancelled attempts in the controlled sample. An initial no-outbox reconciliation is valid, so its batch counts may be zero. These observations show a completed attempt, not current freshness or an empty queue. Keep Shadow and Hybrid Active OFF.
3. **Worker + read-only shadow, separate gate/deployment.** Keep Worker ON and enable Shadow; Hybrid Active remains OFF. Counters reset on the new process. Require `mode=worker_readonly_shadow`, a running worker, available telemetry, and an observed worker completion before the first serialized shadow request. Also require the existing shadow endpoint to report enabled/installed/available. Only then run separately approved representative requests, using existing Memory rather than manufacturing new durable smoke candidates for telemetry. Apply the structural acceptance checks below to the old shadow endpoint, and inspect worker outcomes alongside them.

In this mode, a missing, corrupt, mismatched, half-committed, or stale index pair fails the shadow locally; the query must not repair it. The worker owns eventual reconciliation. Waiting for an observed worker completion reduces startup races but cannot guarantee that authority has not changed again. Shadow success is the query-time evidence. A zero-eligible-vector-document query may legitimately finish without a query embedding.

If acceptance needs proof of the **current** outbox backlog, authoritative membership, or index generations, stop and obtain a separate scoped read-only audit. The status endpoint intentionally cannot provide those proofs. Do not mutate production Memory, force-refresh, delete sidecars, or acknowledge events to make counters green.

## Legacy Stage 0 — code/deployment baseline

The legacy stages require **Worker OFF throughout**, with Hybrid Active also OFF. Only this path permits query-triggered lazy repairs.

Before enabling the shadow:

1. Confirm the deployed branch is `feat/render-telegram-deployment` and includes the reviewed D3B3 observability merge plus this D3B4 configuration contract.
2. Confirm Render Auto-Deploy is OFF.
3. Confirm the pre-existing Memory state above is already healthy.
4. Confirm Worker, Shadow, and Hybrid Active are all `false` in the service environment.
5. Populate all six dedicated Hybrid settings above.
6. Perform one manual deployment with the gate still OFF.
7. Verify normal `/healthz`, `/readyz`, Telegram/Kelivo behavior, and current Memory context behavior are unchanged.
8. Query the authenticated `GET /app/memory/hybrid-shadow/status` endpoint. With the gate OFF it should report `enabled=false`, no in-flight shadow, and zero process-local counters.

This stage proves the rollout scaffolding itself is harmless. It does not validate the embedding provider because D3B2 deliberately does not read Hybrid provider credentials while the gate is OFF.

## Legacy Stage 1 — enable a serialized shadow canary

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

If the new status route is deployed, require `mode=legacy_query_repair`. Do not accept `worker_readonly_shadow` under this legacy procedure; use the worker-backed path instead.

## Structural acceptance checks — either shadow path

For an initial serialized canary, require all of the following from `/app/memory/hybrid-shadow/status` before considering separately approved traffic expansion:

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
- any evidence appears that provider-visible Memory context changed because of the shadow path;
- worker-backed status is unavailable, the worker is unexpectedly not running, or worker failures/cancellations increase during the controlled sample;
- the reported installed mode does not match the explicitly approved startup mode.

Full rollback, with its own gate/deployment approval:

1. Set `MEMORY_INDEX_REFRESH_WORKER_ENABLED=false` **and** `MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED=false`. Keep `MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED=false`.
2. Trigger one manual deployment.
3. Confirm the new process reports `mode=disabled`, `enabled=false`, `task_state=disabled`, and available telemetry. Confirm the shadow status reports disabled and no in-flight shadow. A code version without the new route requires its separately reviewed legacy verification, not an assumed green status.
4. Confirm normal `/readyz` and provider-visible Memory behavior remain healthy.

Do **not** change V1/V2 authority flags as part of rollback. Disposable Hybrid sidecars and outbox records remain in place; with all three Hybrid gates OFF, this rollout does not consume them. Never delete or replace the authoritative `relay.db`, manually complete the outbox, or alter Memory truth as a rollback action. Shutdown cancellation is not a proof of index completion; the next approved worker startup re-proves/reconciles state through its normal path.

## Promotion boundary

A healthy shadow canary does not authorize active Hybrid retrieval. Promotion requires a separate reviewed phase with an explicit provider-visible authority contract, bounded context rendering, rollback semantics, and its own deployment gate.
