"""Generate a replacement in a disposable, tool-free native thread."""
import asyncio
from contextlib import suppress

from .codex_generation_hardening_transport import CodexGenerationHardeningTransport
from .codex_generation_protocol import CodexGenerationError, CodexGenerationProtocol, GENERATION_RPC_METHODS, final_answer_from_turn
from .web_codex_fork import _pin
from .web_session_fork import ForkError


async def generate(runtime, sid, request_id, instruction, passage):
    foundation = runtime.foundation
    if not runtime.generation_enabled:
        raise ForkError("rewrite_unavailable", 503)
    done = asyncio.get_running_loop().create_future()
    thread = turn = final = None

    def receive(method, params):
        nonlocal final
        if thread is None or params.get("threadId") != thread.thread_id:
            return
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("phase") in {"final_answer", "finalAnswer", None}:
                value = final_answer_from_turn({"items": [item]})
                if value is not None:
                    final = (params.get("turnId"), value)
        elif not done.done() and isinstance(params.get("turn"), dict):
            done.set_result(params["turn"])

    async with foundation.activity_gate.generation():
        pin = _pin(runtime.config.store_path, sid)
        scope = foundation.runtime.scope(methods=GENERATION_RPC_METHODS,
            notifications=frozenset({"item/completed", "turn/completed"}), handler=receive)
        protocol = CodexGenerationProtocol(foundation.generation.config, CodexGenerationHardeningTransport(scope))
        try:
            selection = await protocol.qualify(pin["model"], pin["reasoning_effort"])
            thread = await protocol.start_thread(api_session=sid, attempt_id=request_id, persona=instruction,
                                                 selection=selection, ephemeral=True)
            if thread.model_provider != pin["model_provider"]:
                raise ForkError("rewrite_provider_unavailable")
            turn = await protocol.start_turn(thread_id=thread.thread_id, client_message_id=request_id, text=passage,
                                            model=pin["model"], reasoning_effort=pin["reasoning_effort"])
            result = await done
            if result.get("id") != turn.turn_id or result.get("status") != "completed" or result.get("error"):
                raise ForkError("rewrite_output_incomplete", 422)
            text = final_answer_from_turn(result)
            if text is None and final and final[0] == turn.turn_id:
                text = final[1]
            return {"outcome": "success", "text": text}
        finally:
            try:
                async with asyncio.timeout(2):
                    if thread and turn and (not done.done() or done.cancelled()):
                        with suppress(CodexGenerationError):
                            await protocol.interrupt(thread_id=thread.thread_id, turn_id=turn.turn_id)
                    if thread:
                        with suppress(CodexGenerationError):
                            await protocol.unsubscribe(thread_id=thread.thread_id)
            except TimeoutError:
                pass
            finally:
                await scope.close()
