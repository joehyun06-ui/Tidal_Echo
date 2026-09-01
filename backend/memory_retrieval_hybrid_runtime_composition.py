"""Server-owned Hybrid Retrieval shadow composition for Phase 4D-D3B2.

D3B2 supplies the real runner that D3B1 deliberately left unconfigured. The
runner owns only disposable BM25/vector sidecars plus a dedicated embedding
adapter. It never changes provider-visible Memory context or Memory truth.

When sidecars are absent, corrupt, stale, or bound to a previous configured
index identity, one shadow invocation may rebuild both projections from one
proved authoritative Atomic snapshot. All vector embeddings must complete
before either sidecar is written. D3A then independently re-proves the stored
sidecars before the current query may reach the embedding provider.

The C3 vector store records only an embedding model identity and dimensions. To
prevent an endpoint change from silently reusing vectors produced in a different
vector space, D3B2 stores a server-derived synthetic model identity over the
adapter contract + normalized provider base + provider model. The provider still
receives only its original model id; the synthetic identity never leaves the
server.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Mapping

from backend import (
    deployment_config,
    memory_hierarchy_baseline,
    memory_hierarchy_snapshot,
    memory_retrieval_bm25 as bm25,
    memory_retrieval_bm25_store as bm25_store,
    memory_retrieval_embedding_openai as embedding_openai,
    memory_retrieval_hybrid_query as hybrid_query,
    memory_retrieval_hybrid_runtime_shadow as runtime_shadow,
    memory_retrieval_vector as vector,
    memory_retrieval_vector_store as vector_store,
)


COMPOSITION_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-runtime-composition-v1"
BM25_FILENAME: Final = "memory-retrieval-hybrid-bm25-shadow.db"
VECTOR_FILENAME: Final = "memory-retrieval-hybrid-vector-shadow.db"
TERM_KEY_ID_ENV: Final = "MEMORY_HYBRID_BM25_TERM_KEY_ID"
TERM_SECRET_ENV: Final = "MEMORY_HYBRID_BM25_TERM_HMAC_SECRET"
EMBEDDING_API_BASE_ENV: Final = "MEMORY_HYBRID_EMBEDDING_API_BASE"
EMBEDDING_API_KEY_ENV: Final = "MEMORY_HYBRID_EMBEDDING_API_KEY"
EMBEDDING_MODEL_ENV: Final = "MEMORY_HYBRID_EMBEDDING_MODEL"
EMBEDDING_DIMENSIONS_ENV: Final = "MEMORY_HYBRID_EMBEDDING_DIMENSIONS"

_IDENTIFIER_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_MODEL_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_EMBEDDING_IDENTITY_DOMAIN: Final = b"memory-hybrid-embedding-provider-v1\x00"
_REBUILDABLE_QUERY_FAILURES: Final = frozenset({
    "hybrid_query_bm25_invalid",
    "hybrid_query_stale",
    "hybrid_query_vector_invalid",
})
_ERROR_CATEGORIES: Final = frozenset({
    "hybrid_runtime_configuration_invalid",
    "hybrid_runtime_rebuild_failed",
    "hybrid_runtime_source_invalid",
    "memory_retrieval_hybrid_runtime_composition_error",
})


class MemoryRetrievalHybridRuntimeCompositionError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_hybrid_runtime_composition_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except BaseException:
            return "memory_retrieval_hybrid_runtime_composition_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalHybridRuntimeCompositionError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalHybridRuntimeCompositionError(category)


def _exact_env(env: Mapping[str, str], name: str) -> str:
    raw = env.get(name, "")
    if type(raw) is not str or not raw or raw != raw.strip():
        _raise("hybrid_runtime_configuration_invalid")
    try:
        raw.encode("utf-8", errors="strict")
    except UnicodeError:
        _raise("hybrid_runtime_configuration_invalid")
    return raw


def _same_secret(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (UnicodeError, TypeError):
        return left == right


def _protected_secrets(
    env: Mapping[str, str],
    fingerprint_secret: str,
) -> tuple[str, ...]:
    names = (
        "RELAY_SECRET",
        "KELIVO_API_KEY",
        "OPERIT_SHARE_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "CHANNEL_AUDIT_HMAC_SECRET",
        "LLM_API_KEY",
        "LLM_API_KEY_2",
        "LLM_API_KEY_3",
        "LLM_API_KEY_4",
        "MINIMAX_API_KEY",
        "API_LOOP_INTERNAL_TOKEN",
        "API_LOOP_EXPECTED_NONCE",
        "API_LOOP_INSTANCE_NONCE",
    )
    values = [fingerprint_secret]
    values.extend(
        value.strip()
        for name in names
        if type((value := env.get(name, ""))) is str and value.strip()
    )
    return tuple(value for value in values if value)


def _paths_are_separate(
    authority: Path,
    bm25_path: Path,
    vector_path: Path,
    root: Path,
) -> bool:
    try:
        resolved = tuple(
            path.resolve(strict=False)
            for path in (authority, bm25_path, vector_path, root)
        )
        authority_resolved, bm25_resolved, vector_resolved, root_resolved = resolved
        if (
            bm25_resolved == vector_resolved
            or authority_resolved in {bm25_resolved, vector_resolved}
            or root_resolved not in bm25_resolved.parents
            or root_resolved not in vector_resolved.parents
        ):
            return False
        for path in (bm25_path, vector_path):
            if path.is_symlink():
                return False
            if path.exists() and not path.is_file():
                return False
        if authority.exists():
            for path in (bm25_path, vector_path):
                if path.exists() and os.path.samefile(authority, path):
                    return False
        if (
            bm25_path.exists()
            and vector_path.exists()
            and os.path.samefile(bm25_path, vector_path)
        ):
            return False
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _embedding_model_identity(api_base: str, provider_model: str) -> str:
    try:
        material = (
            _EMBEDDING_IDENTITY_DOMAIN
            + embedding_openai.EMBEDDING_ADAPTER_CONTRACT_VERSION.encode("ascii")
            + b"\x00"
            + api_base.encode("ascii")
            + b"\x00"
            + provider_model.encode("ascii")
        )
    except (AttributeError, UnicodeError):
        _raise("hybrid_runtime_configuration_invalid")
    return "hybrid-embed-" + hashlib.sha256(material).hexdigest()[:40]


@dataclass(frozen=True, slots=True, repr=False)
class _BoundEmbeddingCallableV1:
    adapter: embedding_openai.OpenAICompatibleEmbeddingAdapterV1 = field(repr=False)
    provider_model: str = field(repr=False)
    model_identity: str

    def __repr__(self) -> str:
        return "<_BoundEmbeddingCallableV1>"

    async def __call__(
        self,
        texts: tuple[str, ...],
        model: str,
        dimensions: int,
    ) -> object:
        if model != self.model_identity:
            raise embedding_openai.MemoryRetrievalEmbeddingAdapterError(
                "embedding_adapter_configuration_invalid"
            )
        return await self.adapter(texts, self.provider_model, dimensions)


@dataclass(frozen=True, slots=True, repr=False)
class HybridRuntimeConfigV1:
    authority_path: Path = field(repr=False)
    persistent_root: Path = field(repr=False)
    bm25_path: Path = field(repr=False)
    vector_path: Path = field(repr=False)
    fingerprint_key_id: str = field(repr=False)
    fingerprint_hmac_secret: str = field(repr=False)
    max_item_chars: int
    sensitive_storage_enabled: bool
    term_key_id: str = field(repr=False)
    term_hmac_secret: str = field(repr=False)
    embedding_model: str = field(repr=False)
    provider_embedding_model: str = field(repr=False)
    embedding_dimensions: int
    embedding_adapter: _BoundEmbeddingCallableV1 = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<HybridRuntimeConfigV1 "
            f"dimensions={self.embedding_dimensions}>"
        )


def load_hybrid_runtime_config_v1(
    relay_app: object,
    environ: Mapping[str, str] | None = None,
) -> HybridRuntimeConfigV1 | None:
    """Return no configuration when the D3B1 gate is OFF; otherwise fail closed."""

    env = os.environ if environ is None else environ
    if not runtime_shadow.enabled_from_environment(env):
        return None
    try:
        deployment = relay_app.DEPLOYMENT
        memory = deployment.memory
        authority = Path(deployment.db_path).resolve(strict=False)
        root = Path(deployment.persistent_root).resolve(strict=False)
        if not root.is_dir():
            _raise("hybrid_runtime_configuration_invalid")
        bm25_path = root / BM25_FILENAME
        vector_path = root / VECTOR_FILENAME
        if not _paths_are_separate(authority, bm25_path, vector_path, root):
            _raise("hybrid_runtime_configuration_invalid")

        fingerprint_key_id = memory.fingerprint_key_id
        fingerprint_secret = memory.fingerprint_hmac_secret
        if (
            type(fingerprint_key_id) is not str
            or _IDENTIFIER_PATTERN.fullmatch(fingerprint_key_id or "") is None
            or type(fingerprint_secret) is not str
            or not deployment_config.memory_fingerprint_secret_is_strong(
                fingerprint_secret
            )
        ):
            _raise("hybrid_runtime_configuration_invalid")

        term_key_id = _exact_env(env, TERM_KEY_ID_ENV)
        term_secret = _exact_env(env, TERM_SECRET_ENV)
        if (
            _IDENTIFIER_PATTERN.fullmatch(term_key_id) is None
            or term_key_id == fingerprint_key_id
            or not deployment_config.memory_fingerprint_secret_is_strong(term_secret)
        ):
            _raise("hybrid_runtime_configuration_invalid")
        try:
            bm25._validate_term_key(term_key_id, term_secret)
        except bm25.MemoryRetrievalBM25Error:
            _raise("hybrid_runtime_configuration_invalid")

        embedding_base = _exact_env(env, EMBEDDING_API_BASE_ENV)
        embedding_key = _exact_env(env, EMBEDDING_API_KEY_ENV)
        provider_embedding_model = _exact_env(env, EMBEDDING_MODEL_ENV)
        if _MODEL_PATTERN.fullmatch(provider_embedding_model) is None:
            _raise("hybrid_runtime_configuration_invalid")
        dimensions = deployment_config.parse_bounded_int(
            env.get(EMBEDDING_DIMENSIONS_ENV, ""),
            vector.MIN_VECTOR_DIMENSIONS,
            vector.MAX_VECTOR_DIMENSIONS,
            "invalid_memory_hybrid_embedding_dimensions",
        )
        try:
            network_adapter = embedding_openai.OpenAICompatibleEmbeddingAdapterV1(
                embedding_base,
                embedding_key,
            )
        except embedding_openai.MemoryRetrievalEmbeddingAdapterError:
            _raise("hybrid_runtime_configuration_invalid")

        protected = _protected_secrets(env, fingerprint_secret)
        if any(_same_secret(term_secret, candidate) for candidate in protected):
            _raise("hybrid_runtime_configuration_invalid")
        if any(_same_secret(embedding_key, candidate) for candidate in protected):
            _raise("hybrid_runtime_configuration_invalid")
        if _same_secret(term_secret, embedding_key):
            _raise("hybrid_runtime_configuration_invalid")

        embedding_model = _embedding_model_identity(
            network_adapter.api_base,
            provider_embedding_model,
        )
        bound_adapter = _BoundEmbeddingCallableV1(
            adapter=network_adapter,
            provider_model=provider_embedding_model,
            model_identity=embedding_model,
        )

        return HybridRuntimeConfigV1(
            authority_path=authority,
            persistent_root=root,
            bm25_path=bm25_path,
            vector_path=vector_path,
            fingerprint_key_id=fingerprint_key_id,
            fingerprint_hmac_secret=fingerprint_secret,
            max_item_chars=memory.max_item_chars,
            sensitive_storage_enabled=memory.sensitive_storage_enabled,
            term_key_id=term_key_id,
            term_hmac_secret=term_secret,
            embedding_model=embedding_model,
            provider_embedding_model=provider_embedding_model,
            embedding_dimensions=dimensions,
            embedding_adapter=bound_adapter,
        )
    except MemoryRetrievalHybridRuntimeCompositionError:
        raise
    except deployment_config.DeploymentConfigError:
        _raise("hybrid_runtime_configuration_invalid")
    except Exception:
        _raise("hybrid_runtime_configuration_invalid")


def _reader(
    config: HybridRuntimeConfigV1,
) -> memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
    try:
        return memory_hierarchy_snapshot.MemoryHierarchySnapshotReader(
            config.authority_path,
            fingerprint_key_id=config.fingerprint_key_id,
            fingerprint_hmac_secret=config.fingerprint_hmac_secret,
            max_item_chars=config.max_item_chars,
            sensitive_storage_enabled=config.sensitive_storage_enabled,
        )
    except memory_hierarchy_snapshot.MemoryHierarchySnapshotError:
        _raise("hybrid_runtime_configuration_invalid")


def _identity_matches(config: HybridRuntimeConfigV1) -> bool:
    if not _paths_are_separate(
        config.authority_path,
        config.bm25_path,
        config.vector_path,
        config.persistent_root,
    ):
        _raise("hybrid_runtime_configuration_invalid")
    try:
        sparse = bm25_store.load_bm25_store_snapshot(config.bm25_path)
        semantic = vector_store.load_vector_store_snapshot(config.vector_path)
    except (
        bm25_store.MemoryRetrievalBM25StoreError,
        vector_store.MemoryRetrievalVectorStoreError,
    ):
        return False
    return (
        sparse.plan.term_key_id == config.term_key_id
        and semantic.plan.embedding_model == config.embedding_model
        and semantic.plan.dimensions == config.embedding_dimensions
    )


def _prepare_sparse_plan(
    reader: memory_hierarchy_snapshot.MemoryHierarchySnapshotReader,
    config: HybridRuntimeConfigV1,
):
    try:
        snapshot = reader.load_active_snapshot()
        baseline = memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
            snapshot.atomics
        )
        sparse_plan = bm25.build_bm25_index_v1(
            snapshot.atomics,
            source_snapshot_digest=baseline.atomic_snapshot_digest,
            term_key_id=config.term_key_id,
            term_hmac_secret=config.term_hmac_secret,
        )
        return snapshot, baseline.atomic_snapshot_digest, sparse_plan
    except (
        memory_hierarchy_snapshot.MemoryHierarchySnapshotError,
        memory_hierarchy_baseline.MemoryHierarchyBaselineError,
        bm25.MemoryRetrievalBM25Error,
    ):
        _raise("hybrid_runtime_source_invalid")
    except Exception:
        _raise("hybrid_runtime_source_invalid")


def _unlink_disposable(path: Path, config: HybridRuntimeConfigV1) -> None:
    if not _paths_are_separate(
        config.authority_path,
        config.bm25_path,
        config.vector_path,
        config.persistent_root,
    ):
        _raise("hybrid_runtime_configuration_invalid")
    try:
        if path.is_symlink():
            _raise("hybrid_runtime_configuration_invalid")
        if path.exists():
            if not path.is_file():
                _raise("hybrid_runtime_configuration_invalid")
            path.unlink()
    except MemoryRetrievalHybridRuntimeCompositionError:
        raise
    except OSError:
        _raise("hybrid_runtime_rebuild_failed")


def _initialize_bm25(config: HybridRuntimeConfigV1) -> None:
    try:
        bm25_store.initialize_bm25_store(
            config.bm25_path,
            forbidden_paths=(config.authority_path, config.vector_path),
        )
    except bm25_store.MemoryRetrievalBM25StoreError:
        _unlink_disposable(config.bm25_path, config)
        try:
            bm25_store.initialize_bm25_store(
                config.bm25_path,
                forbidden_paths=(config.authority_path, config.vector_path),
            )
        except bm25_store.MemoryRetrievalBM25StoreError:
            _raise("hybrid_runtime_rebuild_failed")


def _initialize_vector(config: HybridRuntimeConfigV1) -> None:
    try:
        vector_store.initialize_vector_store(
            config.vector_path,
            forbidden_paths=(config.authority_path, config.bm25_path),
        )
    except vector_store.MemoryRetrievalVectorStoreError:
        _unlink_disposable(config.vector_path, config)
        try:
            vector_store.initialize_vector_store(
                config.vector_path,
                forbidden_paths=(config.authority_path, config.bm25_path),
            )
        except vector_store.MemoryRetrievalVectorStoreError:
            _raise("hybrid_runtime_rebuild_failed")


def _commit_pair(config: HybridRuntimeConfigV1, sparse_plan, vector_plan) -> None:
    try:
        _initialize_bm25(config)
        _initialize_vector(config)
        sparse_stored = bm25_store.apply_bm25_index_plan(
            config.bm25_path,
            sparse_plan,
        )
        vector_stored = vector_store.apply_vector_index_plan(
            config.vector_path,
            vector_plan,
        )
        if sparse_stored.plan != sparse_plan or vector_stored.plan != vector_plan:
            _raise("hybrid_runtime_rebuild_failed")
    except MemoryRetrievalHybridRuntimeCompositionError:
        raise
    except (
        bm25_store.MemoryRetrievalBM25StoreError,
        vector_store.MemoryRetrievalVectorStoreError,
    ):
        _raise("hybrid_runtime_rebuild_failed")
    except Exception:
        _raise("hybrid_runtime_rebuild_failed")


@dataclass(frozen=True, slots=True, repr=False)
class HybridRetrievalShadowRunnerV1:
    config: HybridRuntimeConfigV1 = field(repr=False)
    reader: memory_hierarchy_snapshot.MemoryHierarchySnapshotReader = field(
        repr=False
    )

    def __repr__(self) -> str:
        return "<HybridRetrievalShadowRunnerV1>"

    async def _rebuild_pair(self) -> None:
        snapshot, digest, sparse_plan = await asyncio.to_thread(
            _prepare_sparse_plan,
            self.reader,
            self.config,
        )
        try:
            semantic_build = await vector.build_vector_index_v1(
                self.config.embedding_adapter,
                snapshot.atomics,
                source_snapshot_digest=digest,
                embedding_model=self.config.embedding_model,
                dimensions=self.config.embedding_dimensions,
            )
        except asyncio.CancelledError:
            raise
        except vector.MemoryRetrievalVectorError:
            _raise("hybrid_runtime_rebuild_failed")
        except Exception:
            _raise("hybrid_runtime_rebuild_failed")
        # No sidecar write occurs until the complete vector provider work above
        # has succeeded for the same authoritative Atomic snapshot.
        await asyncio.to_thread(
            _commit_pair,
            self.config,
            sparse_plan,
            semantic_build.plan,
        )

    async def _query_once(self, query_text: object):
        reference_time = datetime.now(timezone.utc).isoformat()
        return await hybrid_query.fuse_current_hybrid_query_v1(
            self.reader,
            self.config.embedding_adapter,
            query_text=query_text,
            reference_time=reference_time,
            bm25_sidecar_path=self.config.bm25_path,
            term_key_id=self.config.term_key_id,
            term_hmac_secret=self.config.term_hmac_secret,
            vector_sidecar_path=self.config.vector_path,
        )

    async def __call__(self, *, query_text: object):
        rebuilt = False
        if not await asyncio.to_thread(_identity_matches, self.config):
            await self._rebuild_pair()
            rebuilt = True
        try:
            return await self._query_once(query_text)
        except asyncio.CancelledError:
            raise
        except hybrid_query.MemoryRetrievalHybridQueryError as error:
            if rebuilt or error.category not in _REBUILDABLE_QUERY_FAILURES:
                raise
            await self._rebuild_pair()
            return await self._query_once(query_text)


def compose_hybrid_retrieval_shadow_runner_v1(
    relay_app: object,
    environ: Mapping[str, str] | None = None,
) -> HybridRetrievalShadowRunnerV1 | None:
    config = load_hybrid_runtime_config_v1(relay_app, environ)
    if config is None:
        return None
    return HybridRetrievalShadowRunnerV1(config=config, reader=_reader(config))


__all__ = (
    "BM25_FILENAME",
    "COMPOSITION_CONTRACT_VERSION",
    "EMBEDDING_API_BASE_ENV",
    "EMBEDDING_API_KEY_ENV",
    "EMBEDDING_DIMENSIONS_ENV",
    "EMBEDDING_MODEL_ENV",
    "HybridRetrievalShadowRunnerV1",
    "HybridRuntimeConfigV1",
    "MemoryRetrievalHybridRuntimeCompositionError",
    "TERM_KEY_ID_ENV",
    "TERM_SECRET_ENV",
    "compose_hybrid_retrieval_shadow_runner_v1",
    "load_hybrid_runtime_config_v1",
)
