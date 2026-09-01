from __future__ import annotations

import asyncio
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from backend import (
    memory_retrieval_embedding_openai as embedding_openai,
    memory_retrieval_hybrid_query as hybrid_query,
    memory_retrieval_hybrid_runtime_composition as composition,
)


FINGERPRINT_SECRET = "Fingerprint-Secret-0123456789-AbCd!"
TERM_SECRET = "Hybrid-Term-Secret-0123456789-XyZ!"
EMBEDDING_KEY = "Embedding-Key-0123456789-AbCdEfGh!"
PROVIDER_MODEL = "text-embedding-test-v1"


def relay(root: Path):
    memory = types.SimpleNamespace(
        enabled=True,
        context_injection_enabled=True,
        smart_retrieval_enabled=True,
        configuration_valid=True,
        fingerprint_key_id="memory-fingerprint-v1",
        fingerprint_hmac_secret=FINGERPRINT_SECRET,
        max_item_chars=1000,
        sensitive_storage_enabled=False,
    )
    deployment = types.SimpleNamespace(
        db_path=root / "relay.db",
        persistent_root=root,
        memory=memory,
    )
    return types.SimpleNamespace(DEPLOYMENT=deployment)


def enabled_env(**overrides):
    env = {
        "MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED": "true",
        composition.TERM_KEY_ID_ENV: "hybrid-term-v1",
        composition.TERM_SECRET_ENV: TERM_SECRET,
        composition.EMBEDDING_API_BASE_ENV: "https://embedding.example/v1",
        composition.EMBEDDING_API_KEY_ENV: EMBEDDING_KEY,
        composition.EMBEDDING_MODEL_ENV: PROVIDER_MODEL,
        composition.EMBEDDING_DIMENSIONS_ENV: "8",
    }
    env.update(overrides)
    return env


class FakeResponse:
    def __init__(self, payload, *, status=200, headers=None):
        self.status_code = status
        self.headers = {} if headers is None else dict(headers)
        self._raw = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self):
        yield self._raw


class FakeClient:
    def __init__(self, response, capture, **kwargs):
        self.response = response
        self.capture = capture
        self.capture["client_kwargs"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, endpoint, **kwargs):
        self.capture["method"] = method
        self.capture["endpoint"] = endpoint
        self.capture["request"] = kwargs
        return self.response


class EmbeddingAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_is_reordered_by_provider_index_and_secrets_are_hidden(self):
        capture = {}
        response = FakeResponse({
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        })
        adapter = embedding_openai.OpenAICompatibleEmbeddingAdapterV1(
            "https://embedding.example/v1",
            EMBEDDING_KEY,
        )
        with mock.patch.object(
            embedding_openai.httpx,
            "AsyncClient",
            side_effect=lambda **kwargs: FakeClient(response, capture, **kwargs),
        ):
            result = await adapter(("first", "second"), "embed-v1", 2)
        self.assertEqual(result, ([1.0, 0.0], [0.0, 1.0]))
        self.assertEqual(capture["method"], "POST")
        self.assertEqual(
            capture["endpoint"],
            "https://embedding.example/v1/embeddings",
        )
        self.assertEqual(capture["request"]["json"]["input"], ["first", "second"])
        self.assertEqual(capture["request"]["json"]["model"], "embed-v1")
        self.assertEqual(capture["request"]["json"]["dimensions"], 2)
        self.assertFalse(capture["client_kwargs"]["follow_redirects"])
        self.assertFalse(capture["client_kwargs"]["trust_env"])
        self.assertNotIn(EMBEDDING_KEY, repr(adapter))
        self.assertNotIn("embedding.example", repr(adapter))

    async def test_non_200_and_oversized_response_are_fixed_data_free_failures(self):
        private = "PRIVATE-PROVIDER-BODY"
        adapter = embedding_openai.OpenAICompatibleEmbeddingAdapterV1(
            "https://embedding.example/v1",
            EMBEDDING_KEY,
        )
        cases = (
            (
                FakeResponse(private.encode(), status=307),
                "embedding_adapter_request_failed",
            ),
            (
                FakeResponse(
                    {"data": []},
                    headers={
                        "content-length": str(
                            embedding_openai.MAX_RESPONSE_BYTES + 1
                        )
                    },
                ),
                "embedding_adapter_response_invalid",
            ),
        )
        for response, category in cases:
            capture = {}
            with self.subTest(category=category), mock.patch.object(
                embedding_openai.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: FakeClient(response, capture, **kwargs),
            ):
                with self.assertRaises(
                    embedding_openai.MemoryRetrievalEmbeddingAdapterError
                ) as raised:
                    await adapter(("query",), "embed-v1", 2)
            self.assertEqual(raised.exception.category, category)
            self.assertNotIn(private, str(raised.exception))
            self.assertNotIn(private, repr(raised.exception))
            self.assertNotIn(EMBEDDING_KEY, repr(raised.exception))

    def test_remote_plain_http_is_rejected_but_loopback_http_is_allowed(self):
        with self.assertRaises(
            embedding_openai.MemoryRetrievalEmbeddingAdapterError
        ):
            embedding_openai.OpenAICompatibleEmbeddingAdapterV1(
                "http://embedding.example/v1",
                EMBEDDING_KEY,
            )
        adapter = embedding_openai.OpenAICompatibleEmbeddingAdapterV1(
            "http://127.0.0.1:8080/v1",
            EMBEDDING_KEY,
        )
        self.assertEqual(repr(adapter), "<OpenAICompatibleEmbeddingAdapterV1>")


class CompositionConfigTests(unittest.TestCase):
    def test_gate_off_is_exact_config_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = relay(Path(tmp))
            env = {
                "MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED": "false",
                composition.TERM_SECRET_ENV: "definitely-invalid",
                composition.EMBEDDING_API_BASE_ENV: "not-a-url",
            }
            config = composition.load_hybrid_runtime_config_v1(app, env)
            self.assertIsNone(config)
            self.assertFalse((Path(tmp) / composition.BM25_FILENAME).exists())
            self.assertFalse((Path(tmp) / composition.VECTOR_FILENAME).exists())

    def test_enabled_config_uses_dedicated_secrets_and_fixed_sidecar_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = relay(root)
            config = composition.load_hybrid_runtime_config_v1(app, enabled_env())
            self.assertIsNotNone(config)
            self.assertEqual(config.bm25_path, root / composition.BM25_FILENAME)
            self.assertEqual(config.vector_path, root / composition.VECTOR_FILENAME)
            self.assertEqual(config.provider_embedding_model, PROVIDER_MODEL)
            self.assertRegex(config.embedding_model, r"^hybrid-embed-[0-9a-f]{40}$")
            self.assertNotEqual(config.embedding_model, PROVIDER_MODEL)
            self.assertEqual(config.embedding_adapter.model_identity, config.embedding_model)
            self.assertEqual(config.embedding_dimensions, 8)
            for private in (
                FINGERPRINT_SECRET,
                TERM_SECRET,
                EMBEDDING_KEY,
                "embedding.example",
                PROVIDER_MODEL,
            ):
                self.assertNotIn(private, repr(config))
                self.assertNotIn(private, repr(config.embedding_adapter))

    def test_provider_base_and_model_are_part_of_vector_space_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = relay(Path(tmp))
            first = composition.load_hybrid_runtime_config_v1(app, enabled_env())
            base_changed = composition.load_hybrid_runtime_config_v1(
                app,
                enabled_env(**{
                    composition.EMBEDDING_API_BASE_ENV:
                        "https://other-embedding.example/v1"
                }),
            )
            model_changed = composition.load_hybrid_runtime_config_v1(
                app,
                enabled_env(**{
                    composition.EMBEDDING_MODEL_ENV: "text-embedding-test-v2"
                }),
            )
            self.assertNotEqual(first.embedding_model, base_changed.embedding_model)
            self.assertNotEqual(first.embedding_model, model_changed.embedding_model)
            self.assertNotEqual(base_changed.embedding_model, model_changed.embedding_model)

    def test_term_and_embedding_secrets_must_not_reuse_existing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = relay(Path(tmp))
            cases = (
                {composition.TERM_SECRET_ENV: FINGERPRINT_SECRET},
                {composition.EMBEDDING_API_KEY_ENV: FINGERPRINT_SECRET},
                {"LLM_API_KEY": EMBEDDING_KEY},
                {composition.EMBEDDING_API_KEY_ENV: TERM_SECRET},
            )
            for overrides in cases:
                with self.subTest(overrides=tuple(overrides)), self.assertRaises(
                    composition.MemoryRetrievalHybridRuntimeCompositionError
                ) as raised:
                    composition.load_hybrid_runtime_config_v1(
                        app,
                        enabled_env(**overrides),
                    )
                self.assertEqual(
                    raised.exception.category,
                    "hybrid_runtime_configuration_invalid",
                )
                for private in (FINGERPRINT_SECRET, TERM_SECRET, EMBEDDING_KEY):
                    self.assertNotIn(private, repr(raised.exception))

    def test_existing_hardlink_to_authority_fails_before_sidecar_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = relay(root)
            app.DEPLOYMENT.db_path.write_bytes(b"authority")
            bm25_path = root / composition.BM25_FILENAME
            try:
                os.link(app.DEPLOYMENT.db_path, bm25_path)
            except OSError:
                self.skipTest("hard links unavailable")
            with self.assertRaises(
                composition.MemoryRetrievalHybridRuntimeCompositionError
            ) as raised:
                composition.load_hybrid_runtime_config_v1(app, enabled_env())
            self.assertEqual(
                raised.exception.category,
                "hybrid_runtime_configuration_invalid",
            )


class BoundEmbeddingIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_identity_never_reaches_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = composition.load_hybrid_runtime_config_v1(
                relay(Path(tmp)),
                enabled_env(),
            )
        capture = {}

        async def raw_adapter(texts, model, dimensions):
            capture["texts"] = texts
            capture["model"] = model
            capture["dimensions"] = dimensions
            return ([1.0, 0.0],)

        bound = composition._BoundEmbeddingCallableV1(
            adapter=raw_adapter,
            provider_model=PROVIDER_MODEL,
            model_identity=config.embedding_model,
        )
        result = await bound(("query",), config.embedding_model, 2)
        self.assertEqual(result, ([1.0, 0.0],))
        self.assertEqual(capture["texts"], ("query",))
        self.assertEqual(capture["model"], PROVIDER_MODEL)
        self.assertEqual(capture["dimensions"], 2)
        self.assertNotEqual(capture["model"], config.embedding_model)

    async def test_wrong_synthetic_identity_is_rejected_before_provider_call(self):
        calls = 0

        async def raw_adapter(*_args):
            nonlocal calls
            calls += 1
            return ([1.0, 0.0],)

        bound = composition._BoundEmbeddingCallableV1(
            adapter=raw_adapter,
            provider_model=PROVIDER_MODEL,
            model_identity="hybrid-embed-" + "a" * 40,
        )
        with self.assertRaises(
            embedding_openai.MemoryRetrievalEmbeddingAdapterError
        ) as raised:
            await bound(("query",), "hybrid-embed-" + "b" * 40, 2)
        self.assertEqual(
            raised.exception.category,
            "embedding_adapter_configuration_invalid",
        )
        self.assertEqual(calls, 0)


class RunnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _runner(self):
        config = types.SimpleNamespace(
            embedding_adapter=object(),
            embedding_model="embed-v1",
            embedding_dimensions=2,
        )
        return composition.HybridRetrievalShadowRunnerV1(
            config=config,
            reader=types.SimpleNamespace(),
        )

    async def test_vector_failure_occurs_before_any_sidecar_commit(self):
        runner = self._runner()
        snapshot = types.SimpleNamespace(atomics=(object(),))
        with (
            mock.patch.object(
                composition,
                "_prepare_sparse_plan",
                return_value=(snapshot, "a" * 64, object()),
            ),
            mock.patch.object(
                composition.vector,
                "build_vector_index_v1",
                new=mock.AsyncMock(
                    side_effect=composition.vector.MemoryRetrievalVectorError(
                        "embedding_unavailable"
                    )
                ),
            ),
            mock.patch.object(composition, "_commit_pair") as commit,
        ):
            with self.assertRaises(
                composition.MemoryRetrievalHybridRuntimeCompositionError
            ) as raised:
                await runner._rebuild_pair()
        self.assertEqual(raised.exception.category, "hybrid_runtime_rebuild_failed")
        commit.assert_not_called()

    async def test_rebuild_uses_one_snapshot_digest_for_sparse_and_vector(self):
        runner = self._runner()
        snapshot = types.SimpleNamespace(atomics=("atomic-a", "atomic-b"))
        sparse_plan = object()
        vector_plan = object()
        build = types.SimpleNamespace(plan=vector_plan)
        with (
            mock.patch.object(
                composition,
                "_prepare_sparse_plan",
                return_value=(snapshot, "b" * 64, sparse_plan),
            ),
            mock.patch.object(
                composition.vector,
                "build_vector_index_v1",
                new=mock.AsyncMock(return_value=build),
            ) as vector_build,
            mock.patch.object(composition, "_commit_pair") as commit,
        ):
            await runner._rebuild_pair()
        self.assertEqual(vector_build.await_count, 1)
        self.assertEqual(vector_build.await_args.args[1], snapshot.atomics)
        self.assertEqual(
            vector_build.await_args.kwargs["source_snapshot_digest"],
            "b" * 64,
        )
        commit.assert_called_once_with(runner.config, sparse_plan, vector_plan)

    async def test_stale_query_rebuilds_once_then_retries(self):
        runner = self._runner()
        final = object()
        with (
            mock.patch.object(composition, "_identity_matches", return_value=True),
            mock.patch.object(
                composition.HybridRetrievalShadowRunnerV1,
                "_query_once",
                new=mock.AsyncMock(side_effect=[
                    hybrid_query.MemoryRetrievalHybridQueryError(
                        "hybrid_query_stale"
                    ),
                    final,
                ]),
            ) as query_once,
            mock.patch.object(
                composition.HybridRetrievalShadowRunnerV1,
                "_rebuild_pair",
                new=mock.AsyncMock(),
            ) as rebuild,
        ):
            result = await runner(query_text="current query")
        self.assertIs(result, final)
        self.assertEqual(query_once.await_count, 2)
        self.assertEqual(rebuild.await_count, 1)

    async def test_query_embedding_failure_does_not_trigger_full_rebuild(self):
        runner = self._runner()
        with (
            mock.patch.object(composition, "_identity_matches", return_value=True),
            mock.patch.object(
                composition.HybridRetrievalShadowRunnerV1,
                "_query_once",
                new=mock.AsyncMock(
                    side_effect=hybrid_query.MemoryRetrievalHybridQueryError(
                        "hybrid_query_embedding_failed"
                    )
                ),
            ),
            mock.patch.object(
                composition.HybridRetrievalShadowRunnerV1,
                "_rebuild_pair",
                new=mock.AsyncMock(),
            ) as rebuild,
        ):
            with self.assertRaises(hybrid_query.MemoryRetrievalHybridQueryError):
                await runner(query_text="current query")
        rebuild.assert_not_awaited()

    async def test_identity_mismatch_rebuilds_before_first_query_and_never_loops(self):
        runner = self._runner()
        final = object()
        with (
            mock.patch.object(composition, "_identity_matches", return_value=False),
            mock.patch.object(
                composition.HybridRetrievalShadowRunnerV1,
                "_rebuild_pair",
                new=mock.AsyncMock(),
            ) as rebuild,
            mock.patch.object(
                composition.HybridRetrievalShadowRunnerV1,
                "_query_once",
                new=mock.AsyncMock(return_value=final),
            ) as query_once,
        ):
            result = await runner(query_text="current query")
        self.assertIs(result, final)
        rebuild.assert_awaited_once()
        query_once.assert_awaited_once_with("current query")


class StaticWiringTests(unittest.TestCase):
    def test_p3_composes_runner_before_install_and_does_not_enable_gate(self):
        backend_root = Path(__file__).resolve().parents[1]
        text = (backend_root / "p3_relay_app.py").read_text(encoding="utf-8")
        compose = ".compose_hybrid_retrieval_shadow_runner_v1(relay_app)"
        install = "memory_retrieval_hybrid_runtime_shadow.install("
        self.assertIn(compose, text)
        self.assertIn(install, text)
        self.assertLess(text.index(compose), text.index(install))
        self.assertNotIn("MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED=true", text)


if __name__ == "__main__":
    unittest.main()
