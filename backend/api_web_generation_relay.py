"""P3-only API Web admission and canonical completion publication."""
import asyncio

from fastapi import HTTPException

from .api_web_generation import ApiGenerationError, Store


def install(relay):
    if getattr(relay, '_API_GENERATION_CONTROLS_INSTALLED', False):
        return
    store = Store(relay.DB_PATH)
    original_dispatch, original_completion = relay.dispatch_human_message, relay.telegram_completion_for

    async def dispatch(text, *, attachments=None, api_session='', existing_message=None, route=True, extra_meta=None):
        # Telegram, dry/provider probes and Codex keep their existing authorities.
        is_web = extra_meta and extra_meta.get('channel') == 'web' and extra_meta.get('source') == 'relay'
        if is_web and route and existing_message is None and api_session and relay.brain_target() == 'loop':
            state = await asyncio.to_thread(relay.loop_json, '/loop/sessions')
            session = next((s for s in state.get('sessions', []) if s.get('id') == api_session), None)
            if session and session.get('provider') == 'api':
                try:
                    store.initialize()
                    meta = {'user': 'human', 'attachments': attachments or [], 'api_session': api_session, **extra_meta}
                    existing_message = store.accept(api_session, text, meta)
                except ApiGenerationError as error:
                    raise HTTPException(status_code=error.status_code, detail=error.category) from None
        return await original_dispatch(text, attachments=attachments, api_session=api_session,
            existing_message=existing_message, route=route, extra_meta=extra_meta)

    def completion(msg):
        meta = msg.get('meta') or {}
        if meta.get('source') != 'api_generation':
            return original_completion(msg)
        gid = meta.get('generation_id', '')
        try:
            mid = int(gid.removeprefix('api-gen-'))
            if msg.get('kind') != 'reply' or meta.get('provider') != 'api' or meta.get('channel') != 'web' or gid != f'api-gen-{mid}':
                raise ValueError()
            return store.notification(meta.get('api_session'), mid)
        except (ValueError, AttributeError, ApiGenerationError):
            raise HTTPException(status_code=409, detail='generation_response_invalid') from None

    relay.dispatch_human_message, relay.telegram_completion_for = dispatch, completion
    relay._API_GENERATION_CONTROLS_INSTALLED = True
