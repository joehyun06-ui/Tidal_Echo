"""Default-off bounded config projection under the existing cooperative gate."""
from .session_lifecycle import LifecycleError
from .session_lifecycle_history_packet import PrivateHistorySetting


def read_history(adapter,*,access,enabled=False):
    if enabled is not True:raise LifecycleError('lifecycle_disabled')
    if type(adapter) is not PrivateHistorySetting:raise LifecycleError('coordination_invalid')
    adapter._admit(None,access)
    with adapter.session_lock:
        def guard():adapter._admit(None,access)
        def project(legacy,generation):
            count=legacy.load_config().get('history_n')
            if type(count) is not int or not 0<=count<=200:
                raise LifecycleError('coordination_history_read_unavailable')
            return {'history_n':count,'generation':generation,'observed':True}
        return adapter.commands.coordinator.observe_config(project,guard=guard)
