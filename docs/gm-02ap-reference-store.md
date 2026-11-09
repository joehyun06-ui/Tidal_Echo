# GM-02AP: opt-in original-reference IndexedDB store

The example store persists only a strict v1 original reference, keyed by opaque
account/connection identity and conversation ID. No tokens, source text or
confirmation status are stored. Default-off methods do no IO. Save/load validate
and copy references; same-reference replay is idempotent, a different reference
for the same key aborts without overwriting the original. Results resolve only
after transaction completion. Corrupt/unknown schemas fail without repair.

remove and clearIdentity are explicit local cleanup, never cancellation or a
retry permit. The host must gate their use appropriately and clear the correct
account on logout. IndexedDB is same-origin storage, not authentication or
encryption; same-origin trusted code can inspect it. The opaque identity must
include the relevant connection/account scope and must not be a token.

Browser tests cover real reload, scope separation, concurrent identical saves,
conflict preservation, account-only cleanup, corrupt data and default-off IO.
Reloaded references bind the panel only explicitly and do not auto-query or send.
This store is not installed in the production frontend. The host still must
invalidate pending loads after identity/chat changes before binding their result;
there is no automatic restore coordinator here. Browser clearing/eviction means
missing local data, never proof that external dispatch did not happen. No
source-consumption/current-window claim. Default off, production HOLD.
