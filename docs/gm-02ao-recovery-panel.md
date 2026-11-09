# GM-02AO: opt-in recovery DOM panel

mountRecoveryPanel builds a semantic section, live status and one explicit
original-request check button. Default-off mounting has no DOM/network effects.
Binding never fetches. Pending checks disable the button and set aria-busy;
fixed wording separates historical confirmation from current window usability.
Errors retain the original reference, revocation requires rebinding, and rebind/
clear/dispose invalidate late results. Disposal removes only this panel and its
listener, leaving the host container intact. Untrusted response/error strings
are never rendered. There are no send, activate or resend controls.

This remains an example component, not installed into the production frontend.
The host supplies a bounded authenticated status transport and must bind/clear
on identity/connection/chat/version changes. No internal token belongs in the
browser. Cross-refresh reference storage, real auth proxy/DOM/PWA integration,
runtime/provider consumption, production TLS and backups remain open.

The separate offline browser workflow uses the repository's existing pinned
Playwright runner pattern and runs synthetic transport tests in Chromium and
WebKit at a mobile viewport with external requests blocked. It changes no web
tree or production startup/config. Default off, production HOLD.
