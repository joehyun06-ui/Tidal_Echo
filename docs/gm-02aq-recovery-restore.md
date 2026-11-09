# GM-02AQ: opt-in local-reference restore binding

`mountRecoveryRestore(root, {enabled, loadReference, observe})` composes the
existing AN controller/AO panel with an AP store loader. It is a development
example, not installed in the production frontend. Default off creates no DOM
and does no IO, including when bind is called. No new dependencies or workflow.

The host explicitly calls `bind({identity, conversationId, contextVersion})` on
every account, connection, chat, session or relevant version change. Identity
must include account/connection scope and must not contain a token. The opaque
contextVersion must change with the host's relevant session/version context;
it is an invalidation label, not authentication or a persisted revision claim.
Every bind invalidates even the same context (including A -> B -> A), clears
the old panel synchronously and copies the context. The host supplies trusted
loadReference, normally `(identity, chat) => store.load(identity, chat)`.
An optional third `{signal}` argument helps cooperative loaders, but correctness
does not depend on abort support. AP IndexedDB loads already started may finish.

Only a current generation can validate and bind a result. Missing or invalid
references, wrong conversation IDs, rejected storage and unknown schemas never
bind; fixed messages do not expose raw errors. No auto retry, repair, deletion,
save, status query, send, resend or activation exists. Local absence is not
evidence of nondispatch. getState reports local loading phase only, not remote
confirmation, current-window availability, completed reply or retry permission.

The existing button is the sole status-query trigger. Its controller retains
singleflight, original-reference/error and 401/403 behavior. Revocation disables
the button; local loading never rebinds automatically. A host must reauthorize
before explicitly binding again. Loading local data never grants authorization.
observe still must use an authenticated, response-size-bounded transport/proxy;
internal tokens never enter the browser.

clear invalidates pending load and query effects without deleting stored data.
setEnabled(false) also removes the owned DOM. Re-enabling starts idle and does
not reload a previous context. dispose is terminal and preserves sibling DOM.
In-flight operations may settle but cannot restore old UI. The host must gate
all unrelated IO; this example cannot stop another component from using a store.

Real Chromium/WebKit tests use controlled promises to cover reverse-order loads,
A/B/A, identity/chat/version changes, load failures, clear/off/dispose races,
queries completing after changes, revocation and singleflight. Actual IndexedDB
reload/corruption paths remain covered. Loopback/synthetic only, zero external
requests or page errors. No production auth/runtime/provider consumption proof.

Production HOLD. No merge/deploy, production data/config change, real model API,
G3 write or real chat. G3 source/event/revision remain authority; ProjectionCard
is derived. Commercial C is not a daily dependency. Frozen 36/36, 108/108 and
historical OpenAI 7/7 remain unchanged. Full GM-02 acceptance still requires
formal frontend/PWA, real authenticated transport and separate API/Codex runtime,
durability/backup and release verification.
