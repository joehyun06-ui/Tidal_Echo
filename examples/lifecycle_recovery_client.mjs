// Default-off client state controller. Inject an authenticated, bounded status
// transport; this module has no URL, token, fetch, storage, send or activate API.
const refKeys = ['conversation_id', 'activation_request_id', 'attempt_request_id', 'packet_sha256'];
const keys = ['ok', 'state', 'historical', 'revision_applied', 'window_confirmed_at_apply',
  'current_window_confirmed', 'reply_completed', 'retry_authorized'];
const exact = (value, fields) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));

function reference(value) {
  if (!exact(value, refKeys) || typeof value.conversation_id !== 'string'
      || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value.conversation_id)
      || !['activation_request_id', 'attempt_request_id'].every(key => typeof value[key] === 'string'
        && /^[A-Za-z0-9:_-]{1,128}$/.test(value[key]))
      || typeof value.packet_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(value.packet_sha256)) {
    throw new Error('invalid_reference');
  }
  return Object.freeze(Object.fromEntries(refKeys.map(key => [key, value[key]])));
}
export {reference as validateRecoveryReference};

function valid(body) {
  if (!exact(body, keys) || body.ok !== true || body.current_window_confirmed !== false
      || body.reply_completed !== false || body.retry_authorized !== false) return false;
  const confirmed = body.state === 'historical_confirmed';
  return (confirmed || body.state === 'attempt_missing' || body.state === 'unconfirmed')
    && body.historical === confirmed && body.window_confirmed_at_apply === confirmed
    && (confirmed ? Number.isSafeInteger(body.revision_applied) && body.revision_applied > 0
      : body.revision_applied === null);
}

export function createRecoveryController({enabled = false, observe} = {}) {
  const active = enabled === true;
  if (active && typeof observe !== 'function') throw new Error('invalid_transport');
  let disposed = false, generation = 0, identity = null, ref = null, flight = null, abort = null;
  let view = Object.freeze({phase: active ? 'idle' : 'disabled', reference: null});
  const publish = (phase, extra = {}) => (view = Object.freeze({phase, reference: ref, ...extra}));
  function invalidate() { generation++; abort?.abort(); abort = null; flight = null; }
  function clear() { invalidate(); identity = null; ref = null; return publish(active && !disposed ? 'idle' : 'disabled'); }
  return Object.freeze({
    getState: () => view,
    bind(accountIdentity, originalReference) {
      clear();
      if (!active || disposed) return view;
      if (typeof accountIdentity !== 'string' || !accountIdentity || accountIdentity.length > 256) {
        throw new Error('invalid_identity');
      }
      const copy = reference(originalReference);
      identity = accountIdentity; ref = copy;
      return publish('ready');
    },
    clear,
    dispose() { disposed = true; return clear(); },
    check() {
      if (!active || disposed || !ref) return Promise.resolve(view);
      if (flight) return flight;
      const token = generation, account = identity, original = ref;
      abort = new AbortController(); const signal = abort.signal;
      const current = () => generation === token && identity === account && ref === original && !disposed;
      publish('checking');
      const task = (async () => {
        await Promise.resolve();
        if (!current()) return view;
        try {
          const result = await observe(original, {signal});
          if (!current()) return view;
          if (result?.status === 401 || result?.status === 403) {
            invalidate(); identity = null; ref = null; return publish('revoked');
          }
          if (result?.status !== 200 || !valid(result.body)) return publish('unavailable');
          return publish(result.body.state, {historical: result.body.historical,
            revisionApplied: result.body.revision_applied, currentWindowConfirmed: false,
            replyCompleted: false, retryAuthorized: false});
        } catch { return current() ? publish('unavailable') : view; }
        finally { if (current()) { flight = null; abort = null; } }
      })();
      flight = task; return task;
    },
  });
}
