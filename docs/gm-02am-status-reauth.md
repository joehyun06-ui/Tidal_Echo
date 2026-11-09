# GM-02AM: recheck status authentication before delivery

The opt-in status route now binds the initial server-owned AccessScope and
revalidates the original internal-token request after the threaded canonical
observation, immediately before returning its result. Token rotation fails
authentication; changed owner/scope or an invalid mapper result refuses delivery.
Authentication errors are sanitized. Existing initial auth/body/read protections
and query-only behavior remain unchanged. No startup, config, schema or default
changes. Tests cover token/owner/scope changes, invalid/error mapping and a stable
request without modifying canonical rows or JSON.

This is a point recheck, not atomic exclusion of every external authorization
writer or control over bytes already delivered to a client. It does not validate
current G3 source access or confirm current window/provider consumption. Real
runtime adapters, recovery UI, production proxy/TLS and backup acceptance remain
open. No model requests or production writes; production HOLD.
