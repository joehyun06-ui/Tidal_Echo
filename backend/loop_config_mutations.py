"""Pure implementation shared by the existing loop writer and GM-02D candidate.

No file, network or runtime IO. Caller owns load/modify/save coordination.
"""
from copy import deepcopy

from . import deployment_config


class LoopConfigMutationError(ValueError):
    def __init__(self, category):
        super().__init__(category)
        self.category = category


def updated_config(config, body, *, render_mvp, fallback_routes):
    def fail(category): raise LoopConfigMutationError(category)
    try:
        deployment_config.validate_loop_config_update_request(body, render_mvp=render_mvp)
    except deployment_config.DeploymentConfigError as exc:
        raise LoopConfigMutationError(exc.category) from None
    cfg = deepcopy(config)
    if 'history_n' in body:
        cfg['history_n'] = max(0, min(int(body.get('history_n') or 0), 200))
    if isinstance(body.get('main_chain'), list):
        configured = cfg.get('main_chain')
        old = [r for r in configured if isinstance(r, dict) and r.get('url') and r.get('key') and r.get('model')] if isinstance(configured, list) else []
        if not old: old = fallback_routes
        new_chain = []
        for pos, item in enumerate(body['main_chain']):
            if not isinstance(item, dict):
                if render_mvp: fail('invalid_loop_model_route')
                continue
            if render_mvp:
                if not set(item).issubset({'index','model','url','key'}): fail('invalid_loop_model_route')
                if 'index' in item and (not isinstance(item['index'], int) or isinstance(item['index'], bool)):
                    fail('invalid_loop_model_route')
                if any(name in item and not isinstance(item[name], str) for name in ('model','url','key')):
                    fail('invalid_loop_model_route')
            old_idx = int(item.get('index', pos) or 0)
            prev = old[old_idx] if 0 <= old_idx < len(old) else {}
            entry = {
                'model': str(item.get('model') or prev.get('model') or '').strip(),
                'url': str(item.get('url') or prev.get('url') or '').strip().rstrip('/'),
                'key': str(item.get('key') or prev.get('key') or ''),
            }
            if not (entry['model'] and entry['url'] and entry['key']):
                fail(f'row {pos + 1}: model/url/key required')
            new_chain.append(entry)
        if new_chain: cfg['main_chain'] = new_chain
        elif render_mvp: fail('invalid_loop_model_route')
    elif render_mvp and 'main_chain' in body:
        fail('invalid_loop_config_structure')
    if render_mvp:
        try: deployment_config.validate_loop_config_payload(cfg, render_mvp=True)
        except deployment_config.DeploymentConfigError as exc:
            raise LoopConfigMutationError(exc.category) from None
    return cfg
