from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest import mock

import httpx


FAKE_TOKEN = "FAKE_TEST_TOKEN_WITHOUT_BOT_FORMAT"
FAKE_SECRET = "test-webhook-secret"
FAKE_USER = 11001
FAKE_CHAT = 22001


def clear_backend_app_modules():
    """Give synthetic app imports the isolation of a fresh process."""
    module_names = (
        "backend.app", "backend.telegram_integration", "backend.channel_store",
        "backend.kelivo_service", "backend.heartbeat_service",
        "backend.memory_policy", "backend.memory_runtime",
        "backend.memory_store", "backend.memory_service",
        "backend.memory_candidate_review",
        "backend.memory_candidate_review_adapters",
        "backend.memory_candidate_review_composition",
        "backend.memory_explicit_actions", "backend.memory_context",
        "backend.memory_context_integration", "backend.memory_retrieval",
        "backend.memory_formation_extractor",
        "backend.memory_formation_integration",
    )
    for name in module_names:
        sys.modules.pop(name, None)
    package = sys.modules.get("backend")
    if package is not None:
        for name in module_names:
            attribute = name.rsplit(".", 1)[-1]
            if hasattr(package, attribute):
                delattr(package, attribute)


class NoNetworkMixin:
    """Fail the test immediately if production code attempts a real socket."""
    def setUp(self):
        super().setUp()
        import socket
        snapshot = dict(os.environ)
        original_connect = socket.socket.connect

        def restore_environment():
            os.environ.clear()
            os.environ.update(snapshot)

        def blocked(*args, **kwargs):
            raise AssertionError("all real network disabled in tests")

        def internal_socketpair(*args, **kwargs):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                original_connect(client, listener.getsockname())
                server, _ = listener.accept()
                return server, client
            finally:
                listener.close()

        self.addCleanup(restore_environment)
        socketpair_patcher = mock.patch("socket.socketpair", new=internal_socketpair)
        socketpair_patcher.start()
        self.addCleanup(socketpair_patcher.stop)
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex",
            "socket.create_connection", "socket.getaddrinfo",
        ):
            patcher = mock.patch(target, new=blocked)
            patcher.start()
            self.addCleanup(patcher.stop)


def load_app(root: str, *, telegram: bool = True, brain: str = "loop", kelivo: bool = False,
             auto_idempotency: bool = False, operit_share: bool = False,
             memory: bool = False, memory_writes: bool = False,
             memory_sensitive: bool = False, memory_secret: str = "",
             memory_entry: bool = False, memory_context: bool = False,
             memory_smart: bool = False, memory_auto_formation: bool = False,
             memory_candidate_persistence: bool = False,
             memory_candidate_review: bool = False):
    root_path = Path(root)
    brain_path = root_path / "brain_target"
    brain_path.write_text(brain, encoding="utf-8")
    values = {
        "RELAY_SECRET": "test-relay-secret",
        "RELAY_DB": str(root_path / "test-relay.sqlite3"),
        "RELAY_UPLOAD_DIR": str(root_path / "uploads"),
        "RELAY_BRAIN_FILE": str(brain_path),
        "RELAY_LOOP_INGEST_URL": "http://127.0.0.1:9/loop/ingest",
        "TELEGRAM_ENABLED": "true" if telegram else "false",
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
        "TELEGRAM_WEBHOOK_SECRET": FAKE_SECRET,
        "CHANNEL_AUDIT_HMAC_SECRET": "invalid-test-only-audit-secret",
        "TELEGRAM_BOT_ACCOUNT_ID": "test-bot",
        "TELEGRAM_ALLOWED_USER_IDS": str(FAKE_USER),
        "TELEGRAM_ALLOWED_CHAT_IDS": f"{FAKE_CHAT},{FAKE_CHAT + 1}",
        "TELEGRAM_API_BASE": "http://127.0.0.1:9",
        "TELEGRAM_TEST_MODE": "true",
        "TELEGRAM_MAX_TEXT_LENGTH": "32",
        "TELEGRAM_WORKER_POLL_SECONDS": "0.05",
        "KELIVO_ENABLED": "true" if kelivo else "false",
        "KELIVO_API_KEY": "test-kelivo-key-distinct-1234567890",
        "KELIVO_CLIENT_ID": "primary-kelivo",
        "KELIVO_API_SESSION": "shared-test-session",
        "KELIVO_MODEL_ALIAS": "ouou-home",
        "KELIVO_AUTO_IDEMPOTENCY_ENABLED": "true" if auto_idempotency else "false",
        "KELIVO_AUTO_IDEMPOTENCY_REPLAY_SECONDS": "600",
        "OPERIT_SHARE_ENABLED": "true" if operit_share else "false",
        "OPERIT_SHARE_API_KEY": "test-operit-share-key-distinct-1234567890",
        "OPERIT_SHARE_CLIENT_ID": "primary-operit-share",
        "OPERIT_SHARE_MODEL_ALIAS": "ouou-home",
        "HEARTBEAT_ENABLED": "false",
        "HEARTBEAT_SCHEDULE_REVISION": "test-default",
        "MEMORY_CORE_ENABLED": "true" if memory else "false",
        "MEMORY_CONTEXT_INJECTION_ENABLED": "true" if memory_context else "false",
        "MEMORY_SMART_RETRIEVAL_ENABLED": "true" if memory_smart else "false",
        "MEMORY_EXPLICIT_WRITES_ENABLED": "true" if memory_writes else "false",
        "MEMORY_EXPLICIT_ENTRY_ENABLED": "true" if memory_entry else "false",
        "MEMORY_AUTO_FORMATION_ENABLED": "true" if memory_auto_formation else "false",
        "MEMORY_AUTO_CANDIDATE_PERSISTENCE_ENABLED": (
            "true" if memory_candidate_persistence else "false"
        ),
        "MEMORY_CANDIDATE_REVIEW_ENABLED": (
            "true" if memory_candidate_review else "false"
        ),
        "MEMORY_SENSITIVE_STORAGE_ENABLED": "true" if memory_sensitive else "false",
        "MEMORY_MAX_ITEM_CHARS": "1000",
        "MEMORY_FORGET_RETENTION_POLICY": "tombstone_without_content",
        "MEMORY_FINGERPRINT_KEY_ID": "phase1-test-key",
        "MEMORY_FINGERPRINT_HMAC_SECRET": memory_secret,
        "LLM_MODEL": "test-provider-model",
        "LLM_TEMPERATURE": "0.7",
        "LLM_MAX_TOKENS": "2000",
        "API_LOOP_INTERNAL_TOKEN": "test-internal-loop-token-1234567890",
    }
    os.environ.update(values)
    clear_backend_app_modules()
    module = importlib.import_module("backend.app")
    module.telegram_integration = importlib.import_module("backend.telegram_integration")
    module.init_db()
    return module


def update(*, update_id: int = 1, chat_id: int = FAKE_CHAT, user_id: int = FAKE_USER,
           text: str = "hello", chat_type: str = "private", message_id: int = 10) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id, "is_bot": False},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


async def request(module, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def webhook_headers(secret: str = FAKE_SECRET) -> dict[str, str]:
    return {"X-Telegram-Bot-Api-Secret-Token": secret}
