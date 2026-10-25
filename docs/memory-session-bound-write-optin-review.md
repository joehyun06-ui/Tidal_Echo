# Session-bound memory write · opt-in review only (2026-10-25)

**Status: standalone review branch, OFF by default, no API route, no deployment.** This branch is based on the read-only evidence draft, not merged into v48 or the production deployment branch.

## Implemented

- Existing `RememberExplicitMemoryRequest`, normal `remember/correct/forget`, SQLite schema and ledger table remain unchanged.
- Experimental internal `ExplicitMemoryActionService.remember_explicit_user_memory_for_server_session(...)`. Requires `MEMORY_SESSION_BOUND_WRITE_ENABLED=true`, the existing `operator_cli` origin projection, session scope, exactly matching `scope_ref` and server-trusted session ID, and membership in canonical server session rows. Invalid requests fail *before* starting the write.
- `MemoryActionEntryBackend.remember(...canonical_session_id=...)` writes canonical `messages.meta.api_session` together with `channel`/`source` **inside the original atomic unit of work**; the legacy normal entry path sends `None` and preserves previous metadata.
- The existing `MemoryActionRequestBinding` HMAC already covers `scope_type`, `scope_ref`, `request_id`, content, and origin. Cross-session same-request-ID replay conflicts under the unchanged ledger semantics; repeated identical requests are idempotent.
- Read-only evidence projection can recognize newly created session-bound canonical sources after transaction commit. No real content, credentials, or external API calls in tests.

## HARD security boundary

This code is **not** an authentication handler. `trusted_session_id` and `canonical_sessions` are **server-owned inputs** and must NEVER originate directly from JSON, user prompt text, request headers, browser state, unvalidated MCP arguments, or any other client-controlled representation. Membership in canonical sessions alone does not authenticate a multi-tenant requester.

There is currently **no HTTP endpoint or public plugin action calling this new method**. Before wiring it up, review a real authenticated relay/loop request context that binds an allowed principal/session and creates the scoped request within the backend trust boundary. Keep the feature OFF until this review and tenant/role checks are completed. The present single-user relay token is not a substitute for per-tenant authorization.

This branch does **not** attempt the complex correction/forget paths under session binding, legacy backfill, historical time travel, multi-user authorization, source reconstruction, or production deployment. Old unbound canonical records remain untouched and may remain hidden in the evidence panel. An untrusted caller cannot request session writes over any new route because none was added.

## Independent tests

`python -m unittest backend.tests._memory_session_write_worker -v`

Covers disabled-by-default (zero rows), server-owned session validation, role/origin restriction, real SQLite canonical metadata and provenance, cross-session read isolation, replay idempotency and conflict, and unchanged legacy global-user write metadata. The worker runs in a separate process to avoid historical module reload pollution and blocks real network calls. Run the full Python CI and Windows memory CI for this review branch.

## Frozen project scope

v48 acceptance 36/36 and 108/108 remain frozen. The previously verified one-shot live API ledger is not replayed, reset, or opened. No Render deployment, PR merge, schema migration or memory backfill permitted as part of this experiment.
