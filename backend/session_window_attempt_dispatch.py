"""Default-off, synchronous trusted adapter attempt; no live adapter installed.

Persistence precedes invocation. A replay NEVER invokes the adapter. Callback
completion is not evidence of provider receipt, consumption or window success.
The supplied authority must fence relevant writers across the external effect.
"""
import inspect

from .session_lifecycle import LifecycleError
from .session_window_attempt_validation import record_validated_attempt


def attempt_dispatch_once(engine, journal, cid, request_id, *, packet_sha256,
                          expected_revision, access, dispatch, enabled=False):
    if enabled is not True:
        raise LifecycleError('lifecycle_disabled')
    # Trusted synchronous function only. Do not accidentally create an unawaited
    # async/generator operation and then misreport an attempt as completed.
    if (not inspect.isfunction(dispatch) or inspect.iscoroutinefunction(dispatch)
            or inspect.isgeneratorfunction(dispatch) or inspect.isasyncgenfunction(dispatch)):
        raise LifecycleError('lifecycle_dispatch_adapter_invalid')

    def invoke(observation):
        try:
            value = dispatch(observation)
            if inspect.isawaitable(value) or inspect.isgenerator(value) or inspect.isasyncgen(value):
                if inspect.iscoroutine(value) or inspect.isgenerator(value):
                    value.close()
                raise LifecycleError('lifecycle_dispatch_unknown')
        except Exception:
            # The row is already durable. Errors (even local validation errors)
            # cannot prove that an external effect did not occur. Never retry.
            raise LifecycleError('lifecycle_dispatch_unknown') from None

    return record_validated_attempt(engine, journal, cid, request_id,
        packet_sha256=packet_sha256, expected_revision=expected_revision, access=access,
        enabled=True, _on_recorded=invoke)
