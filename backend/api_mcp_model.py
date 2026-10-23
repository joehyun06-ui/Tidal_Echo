"""Bounded API function calls for explicitly opted-in Web sessions."""
import asyncio
import json
import re
from .web_mcp_chat import McpError, prepare
from . import usage_ledger_capture

MAX_RESPONSE = 2 * 1024 * 1024
SAFE_ID = re.compile(r'^[A-Za-z0-9_.:-]{1,160}$')


async def bounded_lines(response):
    total, pending = 0, b''
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE:
            raise McpError('model_response_too_large')
        pending += chunk
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            yield line.decode('utf-8').rstrip('\r')
    if pending:
        yield pending.decode('utf-8')


def add_usage(total, value):
    if not isinstance(value, dict):
        return
    for key, number in value.items():
        if type(number) is int and 0 <= number < 10**15:
            total[key] = total.get(key, 0) + number if type(total.get(key, 0)) is int else number
        elif isinstance(number, dict):
            if not isinstance(total.get(key), dict):
                total[key] = {}
            add_usage(total[key], number)


def parse_calls(values, responses=False):
    if not isinstance(values, list) or len(values) > 4:
        raise McpError('invalid_model_tools')
    calls = []
    for item in values:
        if not isinstance(item, dict) or item.get('type') != ('function_call' if responses else 'function'):
            raise McpError('invalid_model_tools')
        fn = item if responses else item.get('function', {})
        name, cid, raw = fn.get('name'), item.get('call_id' if responses else 'id'), fn.get('arguments')
        if not isinstance(cid, str) or not SAFE_ID.fullmatch(cid) or not isinstance(name, str) or not isinstance(raw, str) or len(raw.encode()) > 16000:
            raise McpError('invalid_model_tools')
        args = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(args, dict):
            raise McpError('invalid_model_tools')
        calls.append((cid, name, args))
    if len({c[0] for c in calls}) != len(calls):
        raise McpError('invalid_model_tools')
    return calls


@usage_ledger_capture.observe_api(route_index=1)
async def round_trip(legacy, route, messages, definitions, sink, *, responses):
    streaming = bool(legacy.STREAM_OUTPUT)
    body = legacy._responses_body(route, messages, stream=streaming) if responses else legacy._chat_completion_body(route, messages, stream=streaming)
    body['tools'] = [{'type': 'function', **d} if responses else {'type': 'function', 'function': d} for d in definitions]
    body['parallel_tool_calls'] = False
    if responses:
        body['include'] = ['reasoning.encrypted_content']
    body = usage_ledger_capture.prepare_body(body, responses=responses)
    size, text, usage, calls, finished, output, done = 0, '', {}, {}, False, None, False
    async def emit(chunk):
        nonlocal text
        if not isinstance(chunk, str) or len(text) + len(chunk) > legacy.LOOP_ASSISTANT_MAX_CHARS:
            raise McpError('model_text_invalid')
        text += chunk
        if chunk:
            await sink(chunk)
    async with asyncio.timeout(legacy.LOOP_MODEL_TOTAL_TIMEOUT_SECONDS):
        async with legacy._provider_client(timeout=legacy.LOOP_MODEL_TOTAL_TIMEOUT_SECONDS, trust_env=False) as client:
            async with client.stream('POST', route['url'].rstrip('/') + ('/responses' if responses else '/chat/completions'),
                                     headers={'Authorization': 'Bearer ' + route['key'], 'Content-Type': 'application/json'}, json=body) as resp:
                usage_ledger_capture.response_status(resp.status_code)
                await legacy._check_provider_response(resp)
                if not streaming:
                    raw = bytearray()
                    async for chunk in resp.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE:
                            raise McpError('model_response_too_large')
                    data = json.loads(raw)
                    usage_ledger_capture.reported_model(data.get('model'))
                    usage = data.get('usage') or {}
                    usage_ledger_capture.usage(data.get('usage'))
                    if responses:
                        if data.get('status') != 'completed':
                            raise McpError('incomplete_model_response')
                        output = data.get('output', [])
                        await emit(legacy._responses_text(data))
                    else:
                        choice = data['choices'][0]
                        if choice.get('finish_reason') not in ('stop', 'tool_calls'):
                            raise McpError('incomplete_model_response')
                        msg = choice['message']
                        output = {'role': 'assistant', 'content': msg.get('content') or None}
                        if msg.get('tool_calls'):
                            output['tool_calls'] = msg['tool_calls']
                        await emit(msg.get('content') or '')
                    finished = done = True
                else:
                    async for line in bounded_lines(resp):
                        size += len(line.encode())
                        if size > MAX_RESPONSE:
                            raise McpError('model_response_too_large')
                        if not line.startswith('data:'):
                            continue
                        raw = line[5:].strip()
                        if raw == '[DONE]':
                            done = True
                            break
                        if not raw:
                            continue
                        event = json.loads(raw)
                        if not isinstance(event, dict):
                            raise McpError('invalid_model_response')
                        if responses:
                            kind = event.get('type')
                            if kind == 'response.output_text.delta':
                                await emit(event.get('delta'))
                            elif kind == 'response.completed':
                                data = event.get('response', {})
                                usage_ledger_capture.reported_model(data.get('model'))
                                if data.get('status') not in (None, 'completed'):
                                    raise McpError('incomplete_model_response')
                                output, usage = data.get('output', []), data.get('usage') or {}
                                usage_ledger_capture.usage(data.get('usage'))
                                if not text:
                                    await emit(legacy._responses_text(data))
                                finished = done = True
                                break
                            elif kind in ('response.failed', 'response.incomplete', 'error'):
                                data = event.get('response')
                                if isinstance(data, dict): usage_ledger_capture.usage(data.get('usage'))
                                raise McpError('incomplete_model_response')
                        else:
                            usage_ledger_capture.reported_model(event.get('model'))
                            usage_ledger_capture.usage(event.get('usage'))
                            if isinstance(event.get('usage'), dict):
                                usage = event['usage']
                            choices = event.get('choices')
                            if not isinstance(choices, list):
                                raise McpError('invalid_model_response')
                            if not choices:
                                continue
                            if len(choices) != 1:
                                raise McpError('invalid_model_response')
                            choice = choices[0]; delta = choice.get('delta') or {}
                            if delta.get('content'):
                                await emit(delta['content'])
                            for part in delta.get('tool_calls', []):
                                index = part.get('index')
                                if type(index) is not int or not 0 <= index < 4:
                                    raise McpError('invalid_model_tools')
                                target = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                                if part.get('type', 'function') != 'function':
                                    raise McpError('invalid_model_tools')
                                if 'id' in part:
                                    target['id'] += part['id']
                                for key in ('name', 'arguments'):
                                    if key in part.get('function', {}):
                                        target['function'][key] += part['function'][key]
                            if choice.get('finish_reason') is not None:
                                if choice['finish_reason'] not in ('stop', 'tool_calls'):
                                    raise McpError('incomplete_model_response')
                                finished = True
                    if not responses:
                        output = {'role': 'assistant', 'content': text or None}
                        if calls:
                            output['tool_calls'] = [calls[k] for k in sorted(calls)]
    if not finished or not done:
        raise McpError('incomplete_model_response')
    if responses:
        if not isinstance(output, list) or any(not isinstance(v, dict) or v.get('type') not in ('message', 'reasoning', 'function_call') for v in output):
            raise McpError('invalid_model_response')
        requested = parse_calls([v for v in output if v.get('type') == 'function_call'], True)
    else:
        requested = parse_calls(output.get('tool_calls', []))
    if not text and not requested:
        raise McpError('empty_model_response')
    return text, usage, output, requested


async def run(legacy, route, messages, context, sink):
    responses = legacy._uses_responses_api(route)
    history, all_text, usage, seen, reports, requests = list(messages), '', {}, set(), 0, 0
    async def emit(chunk):
        nonlocal all_text
        if len(all_text) + len(chunk) > legacy.LOOP_ASSISTANT_MAX_CHARS:
            raise McpError('model_text_too_large')
        all_text += chunk
        await sink(chunk)
    try:
        for iteration in range(5):
            requests += 1
            if all_text:
                await emit('\n\n')
            text, round_usage, output, calls = await round_trip(legacy, route, history, context.definitions(), emit, responses=responses)
            if round_usage:
                reports += 1
            add_usage(usage, round_usage)
            if not calls:
                return {'outcome': 'success', 'text': all_text, 'model': route['model'], 'usage': usage,
                        'usage_complete': reports == requests, 'tried': []}
            if iteration == 4 or len(seen) + len(calls) > 8 or any(c[0] in seen for c in calls):
                raise McpError('tool_limit')
            history.extend(output if responses else [output])
            for cid, name, arguments in calls:
                seen.add(cid)
                result = await context.invoke(name, arguments)
                history.append({'type': 'function_call_output', 'call_id': cid, 'output': result} if responses
                               else {'role': 'tool', 'tool_call_id': cid, 'content': result})
    except asyncio.CancelledError:
        raise
    except Exception:
        # Never retry/fallback inference after tools may have run.
        return {'outcome': 'dispatch_uncertain', 'text': all_text, 'model': route['model'],
                'usage': usage, 'usage_complete': False, 'tried': [], 'error': 'api_mcp_incomplete'}


async def generate(legacy, sid, mid, messages, sink, on_route):
    try:
        context = await asyncio.to_thread(prepare, legacy.RELAY_DB, sid, mid)
    except asyncio.CancelledError:
        raise
    except Exception:
        return {'outcome': 'explicit_failed', 'error': 'api_mcp_unavailable', 'usage_complete': False}
    if context is None:
        return await legacy.run_model(messages, stream_id=f'api-gen-{mid}', session_id=sid,
                                      emit_stream=True, progress_sink=sink, on_route=on_route)
    routes = legacy.main_chain()
    if not routes:
        raise McpError('model_unavailable')
    route = routes[0]
    on_route(route)
    return await run(legacy, route, messages, context, sink)
