# GM-02AN: default-off recovery client state controller

The dependency-free example ES module is an opt-in controller for a future UI,
not an installed frontend or rendered recovery panel. It accepts an injected
authenticated, response-bounded status transport and opaque account identity;
it has no URL/token/fetch/storage/send/activate operation. Binding or opening it
does not fetch; check() is an explicit user action. Concurrent checks share one
request. References are copied/frozen and retained on network/409/503/malformed
responses; no automatic retries or new request IDs are created.

Only exact conservative status contracts are accepted. Historical confirmation
never implies current window availability, completed reply or retry permission.
Identity/chat rebinding, clear and disposal abort pending work and invalidate
late responses even if the transport ignores abort. A 401/403 clears the scoped
reference and requires explicit rebinding. Invalid rebinding withdraws old state.
The UI owner must call bind/clear on every connection/identity/session change;
the controller cannot observe application state changes on its own.

Eight Node tests cover response claims, original references, concurrent requests,
late results, revocation and errors; the Python suite invokes them via Node.
No npm dependencies, frontend/web tree edits or browser integration are added.
Actual frontend DOM/PWA/auth proxy integration, cross-refresh reference storage,
real runtime/provider consumption and recovery UX acceptance remain open.
Default off; production HOLD and frozen baselines unchanged.
