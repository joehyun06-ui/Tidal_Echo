from __future__ import annotations

import dataclasses
import importlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import (
    channel_store,
    deployment_config,
    memory_policy,
    memory_runtime,
    memory_store,
)
from backend.tests._support import NoNetworkMixin


TEST_SECRET = "Synthetic-Authority-HMAC-Key-2026-Alpha!Z9q7"
STATE_TABLES = (
    "memory_items",
    "memory_sources",
    "memory_suppressions",
    "memory_evidence_events",
    "memory_fingerprint_profile",
)


def config(
    *,
    enabled: bool = True,
    writes: bool = True,
    sensitive: bool = False,
    secret: str = TEST_SECRET,
    key_id: str = "authority-test-key",
) -> deployment_config.MemoryConfig:
    return deployment_config.MemoryConfig(
        enabled=enabled,
        explicit_writes_enabled=writes,
        sensitive_storage_enabled=sensitive,
        max_item_chars=1000,
        forget_retention_policy="tombstone_without_content",
        fingerprint_key_id=key_id,
        fingerprint_hmac_secret=secret,
        configuration_valid=bool(secret) or not enabled,
        error_category="" if secret or not enabled else "memory_configuration_invalid",
    )


def bootstrap(path: str, runtime_config: deployment_config.MemoryConfig):
    global channel_store, memory_policy, memory_runtime, memory_store
    memory_runtime = importlib.import_module("backend.memory_runtime")
    memory_runtime = importlib.reload(memory_runtime)
    channel_store = importlib.import_module("backend.channel_store")
    memory_policy = importlib.import_module("backend.memory_policy")
    memory_store = importlib.import_module("backend.memory_store")
    deployment = SimpleNamespace(memory=runtime_config, db_path=Path(path))
    with mock.patch.object(
        deployment_config,
        "load_deployment_config",
        return_value=deployment,
    ):
        return memory_runtime.bootstrap_memory_runtime_from_environment(object())


class MemoryAuthorityTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "authority.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
        channel_store.run_migrations(self.path)
        self.runtime = bootstrap(self.path, config())
        self.read = self.runtime.read_service
        self.actions = self.runtime.privileged_actions
        self.store = self.actions._store

    def message(
        self,
        *,
        direction: str = "in",
        kind: str = "user",
        channel: str = "web",
        source: str = "relay",
    ) -> int:
        with channel_store.connect(self.path) as conn:
            return int(conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,?,?,?,?)""",
                (
                    channel_store.now_iso(),
                    direction,
                    kind,
                    "synthetic authority action",
                    json.dumps({"channel": channel, "source": source}),
                ),
            ).lastrowid)

    def counts(self) -> dict[str, int]:
        with channel_store.connect(self.path) as conn:
            return {
                table: int(conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0])
                for table in STATE_TABLES
            }

    def assert_zero_state(self):
        self.assertEqual(self.counts(), {table: 0 for table in STATE_TABLES})

    def binding(
        self,
        message_id: int,
        *,
        content: str = "Synthetic authority-bound memory",
        action_type: str = memory_runtime.ACTION_REMEMBER_USER,
        kind: str = "project",
        scope_type: str = "global_user",
        scope_ref: str = "",
        sensitivity: str = "normal",
        memory_key: str = "",
    ):
        return memory_runtime.MemoryActionBinding(
            action_type=action_type,
            canonical_message_id=message_id,
            kind=kind,
            scope_type=scope_type,
            scope_ref=scope_ref,
            normalized_content=memory_policy.normalize_content(
                content, max_chars=1000,
            ),
            sensitivity=sensitivity,
            memory_key=memory_key,
        )

    def direct_create(
        self,
        message_id: int,
        authorization: object | None,
        *,
        content: str = "Synthetic authority-bound memory",
    ):
        return self.store.create_explicit_memory_from_user_action(
            kind="project",
            scope_type="global_user",
            scope_ref="",
            content=content,
            sensitivity="normal",
            sources=(memory_policy.ProvenanceInput(message_id),),
            authorization=authorization,
        )

    def test_caller_config_injection_and_fake_authority_are_rejected(self):
        @dataclasses.dataclass(frozen=True)
        class FakeAuthority:
            enabled: bool = True
            explicit_writes_enabled: bool = True
            sensitive_storage_enabled: bool = True
            trusted: bool = True

        for fake in (config(sensitive=True), FakeAuthority(), object()):
            with self.subTest(fake_type=type(fake).__name__), self.assertRaisesRegex(
                memory_store.MemoryStoreError, "runtime_authority_invalid",
            ):
                memory_store.MemoryStore(self.path, fake)
        self.assert_zero_state()

    def test_environment_mutation_cannot_enable_frozen_disabled_runtime(self):
        disabled = bootstrap(
            self.path,
            config(enabled=False, writes=False, secret=""),
        )
        before = self.counts()
        with mock.patch.dict(os.environ, {
            "MEMORY_CORE_ENABLED": "true",
            "MEMORY_EXPLICIT_WRITES_ENABLED": "true",
            "MEMORY_SENSITIVE_STORAGE_ENABLED": "true",
            "MEMORY_FINGERPRINT_HMAC_SECRET": "attacker-replacement",
        }):
            with self.assertRaisesRegex(Exception, "feature_disabled"):
                disabled.privileged_actions.remember_explicit_user_message(
                    kind="project",
                    scope_type="global_user",
                    scope_ref="",
                    content="Synthetic disabled memory",
                    sensitivity="normal",
                    canonical_message_id=self.message(),
                )
        self.assertEqual(self.counts(), before)

    def test_second_bootstrap_cannot_replace_process_authority(self):
        with mock.patch.object(
            deployment_config,
            "load_deployment_config",
            return_value=SimpleNamespace(
                memory=config(
                    sensitive=True,
                    secret="Attacker-Replacement-HMAC-Key-2026!Q8w7",
                    key_id="attacker-key",
                ),
                db_path=Path(self.path),
            ),
        ) as loader:
            with self.assertRaisesRegex(
                memory_runtime.MemoryRuntimeError,
                "memory_runtime_already_initialized",
            ):
                memory_runtime.bootstrap_memory_runtime_from_environment(object())
        loader.assert_not_called()
        self.assert_zero_state()

    def test_read_service_and_ordinary_canonical_row_have_no_write_grant(self):
        message_id = self.message()
        for name in (
            "create_explicit_memory",
            "correct_memory",
            "forget_memory",
            "remember_explicit_user_message",
        ):
            self.assertFalse(hasattr(self.read, name))
        self.assertFalse(hasattr(self.read, "_authority"))
        self.assertFalse(hasattr(self.read, "_store"))
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "runtime_authority_invalid",
        ):
            memory_store.MemoryStore(self.path, self.read._reader)
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "authorization_required",
        ):
            self.direct_create(message_id, None)
        self.assert_zero_state()

    def test_forged_capability_is_rejected_without_side_effects(self):
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "authorization_invalid",
        ):
            self.direct_create(self.message(), object())
        self.assert_zero_state()

    def test_every_bound_field_tamper_is_rejected_without_side_effects(self):
        message_id = self.message()
        binding = self.binding(message_id)
        envelope = memory_runtime.issue_action_envelope(
            self.actions._authority, binding,
        )
        mutations = {
            "canonical_message_id": message_id + 1,
            "normalized_content": "Synthetic changed content",
            "kind": "relationship",
            "scope_type": "project",
            "scope_ref": "changed",
            "sensitivity": "sensitive",
            "memory_key": "A" * 32,
            "action_type": memory_runtime.ACTION_CORRECT_USER,
        }
        for field, value in mutations.items():
            tampered = dataclasses.replace(
                envelope,
                binding=dataclasses.replace(binding, **{field: value}),
            )
            with self.subTest(field=field), self.assertRaisesRegex(
                memory_store.MemoryStoreError, "authorization_invalid",
            ):
                self.direct_create(message_id, tampered)
            self.assert_zero_state()

    def test_capability_cannot_cross_content_kind_scope_or_action_purpose(self):
        message_id = self.message()
        envelope = memory_runtime.issue_action_envelope(
            self.actions._authority,
            self.binding(message_id),
        )
        attempts = (
            lambda: self.direct_create(
                message_id, envelope, content="Synthetic different memory",
            ),
            lambda: self.store.create_explicit_memory_from_user_action(
                kind="relationship",
                scope_type="global_user",
                scope_ref="",
                content="Synthetic authority-bound memory",
                sensitivity="normal",
                sources=(memory_policy.ProvenanceInput(message_id),),
                authorization=envelope,
            ),
            lambda: self.store.create_explicit_memory_from_user_action(
                kind="project",
                scope_type="project",
                scope_ref="changed",
                content="Synthetic authority-bound memory",
                sensitivity="normal",
                sources=(memory_policy.ProvenanceInput(message_id),),
                authorization=envelope,
            ),
            lambda: self.store.create_assistant_experience_from_action(
                kind="assistant_experience",
                scope_type="global_user",
                scope_ref="",
                content="Synthetic authority-bound memory",
                sensitivity="normal",
                sources=(memory_policy.ProvenanceInput(message_id),),
                authorization=envelope,
            ),
        )
        for index, attempt in enumerate(attempts):
            with self.subTest(index=index), self.assertRaisesRegex(
                memory_store.MemoryStoreError, "authorization_invalid",
            ):
                attempt()
            self.assert_zero_state()

        assistant_message_id = self.message(
            direction="out",
            kind="reply",
            channel="galatea",
            source="assistant_runtime",
        )
        assistant_envelope = memory_runtime.issue_action_envelope(
            self.actions._authority,
            self.binding(
                assistant_message_id,
                action_type=memory_runtime.ACTION_ASSISTANT_EXPERIENCE,
                kind="assistant_experience",
            ),
        )
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "authorization_invalid",
        ):
            self.direct_create(message_id, assistant_envelope)
        self.assert_zero_state()

    def test_user_action_cannot_reclassify_assistant_memory(self):
        assistant_message = self.message(
            direction="out",
            kind="reply",
            channel="galatea",
            source="assistant_runtime",
        )
        created = self.actions.record_assistant_experience(
            scope_type="global_user",
            scope_ref="",
            content="Synthetic assistant experience",
            sensitivity="normal",
            canonical_message_id=assistant_message,
        )
        memory = created["memory"]
        before = self.counts()
        correction_message = self.message()
        correction_binding = memory_runtime.MemoryActionBinding(
            action_type=memory_runtime.ACTION_CORRECT_USER,
            canonical_message_id=correction_message,
            kind=memory["kind"],
            scope_type=memory["scope_type"],
            scope_ref=memory["scope_ref"],
            normalized_content="Synthetic user rewrite",
            sensitivity="normal",
            memory_key=memory["memory_key"],
        )
        envelope = memory_runtime.issue_action_envelope(
            self.actions._authority, correction_binding,
        )
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "unsupported_evidence",
        ):
            self.store.correct_memory_from_user_action(
                memory_key=memory["memory_key"],
                content="Synthetic user rewrite",
                sensitivity="normal",
                sources=(memory_policy.ProvenanceInput(correction_message),),
                authorization=envelope,
            )
        self.assertEqual(self.counts(), before)

    def test_successful_capability_is_consumed_once_and_audited(self):
        message_id = self.message()
        envelope = memory_runtime.issue_action_envelope(
            self.actions._authority, self.binding(message_id),
        )
        created = self.direct_create(message_id, envelope)
        after = self.counts()
        self.assertEqual(created.outcome, "created")
        self.assertEqual(after, {
            "memory_items": 1,
            "memory_sources": 1,
            "memory_suppressions": 0,
            "memory_evidence_events": 1,
            "memory_fingerprint_profile": 1,
        })
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "authorization_replayed",
        ):
            self.direct_create(message_id, envelope)
        self.assertEqual(self.counts(), after)
        with channel_store.connect(self.path) as conn:
            audit = conn.execute(
                """SELECT action_type,action_binding_version
                   FROM memory_evidence_events"""
            ).fetchone()
        self.assertEqual(
            tuple(audit),
            (memory_runtime.ACTION_REMEMBER_USER, 1),
        )

    def test_expired_and_previous_process_capabilities_are_rejected(self):
        message_id = self.message()
        expired = memory_runtime.issue_action_envelope(
            self.actions._authority,
            self.binding(message_id),
            now_ns=lambda: 1,
        )
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "authorization_expired",
        ):
            self.direct_create(message_id, expired)
        self.assert_zero_state()

        prior_process = memory_runtime.issue_action_envelope(
            self.actions._authority, self.binding(message_id),
        )
        replacement = bootstrap(self.path, config())
        replacement_store = replacement.privileged_actions._store
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "authorization_invalid",
        ):
            replacement_store.create_explicit_memory_from_user_action(
                kind="project",
                scope_type="global_user",
                scope_ref="",
                content="Synthetic authority-bound memory",
                sensitivity="normal",
                sources=(memory_policy.ProvenanceInput(message_id),),
                authorization=prior_process,
            )
        self.assert_zero_state()

    def test_rolled_back_action_is_not_consumed_and_leaves_no_orphans(self):
        message_id = self.message()
        envelope = memory_runtime.issue_action_envelope(
            self.actions._authority, self.binding(message_id),
        )
        with mock.patch.object(
            self.store,
            "_insert_sources",
            side_effect=memory_store.MemoryStoreError("injected_failure"),
        ):
            with self.assertRaisesRegex(
                memory_store.MemoryStoreError, "injected_failure",
            ):
                self.direct_create(message_id, envelope)
        self.assert_zero_state()
        self.assertEqual(self.direct_create(message_id, envelope).outcome, "created")

    def test_suppression_consumes_capability_without_audit_or_profile_orphans(self):
        content = "Synthetic suppressed authority memory"
        created = self.actions.remember_explicit_user_message(
            kind="project",
            scope_type="global_user",
            scope_ref="",
            content=content,
            sensitivity="normal",
            canonical_message_id=self.message(),
        )
        self.actions.forget_explicit_user_memory(
            memory_key=created["memory"]["memory_key"],
            canonical_message_id=self.message(),
        )
        before = self.counts()
        message_id = self.message()
        envelope = memory_runtime.issue_action_envelope(
            self.actions._authority,
            self.binding(message_id, content=content),
        )
        self.assertEqual(
            self.direct_create(message_id, envelope, content=content).outcome,
            "suppressed",
        )
        self.assertEqual(self.counts(), before)
        with self.assertRaisesRegex(
            memory_store.MemoryStoreError, "authorization_replayed",
        ):
            self.direct_create(message_id, envelope, content=content)
        self.assertEqual(self.counts(), before)

    def test_concurrent_consume_of_one_capability_has_one_winner(self):
        message_id = self.message()
        envelope = memory_runtime.issue_action_envelope(
            self.actions._authority, self.binding(message_id),
        )

        def consume():
            try:
                return self.direct_create(message_id, envelope).outcome
            except memory_store.MemoryStoreError as error:
                return error.category

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _index: consume(), range(8)))
        self.assertEqual(outcomes.count("created"), 1)
        self.assertEqual(outcomes.count("authorization_replayed"), 7)
        self.assertEqual(self.counts(), {
            "memory_items": 1,
            "memory_sources": 1,
            "memory_suppressions": 0,
            "memory_evidence_events": 1,
            "memory_fingerprint_profile": 1,
        })

    def test_runtime_and_capability_repr_and_errors_do_not_disclose_values(self):
        message_id = self.message()
        binding = self.binding(message_id)
        envelope = memory_runtime.issue_action_envelope(
            self.actions._authority, binding,
        )
        rendered = " ".join((
            repr(self.runtime),
            repr(self.actions._authority),
            repr(envelope),
            repr(binding),
        ))
        for forbidden in (
            TEST_SECRET,
            "authority-test-key",
            "Synthetic authority-bound memory",
            "canonical_message_id",
            "scope_ref",
        ):
            self.assertNotIn(forbidden, rendered)
        try:
            self.direct_create(message_id, object())
        except memory_store.MemoryStoreError as error:
            self.assertEqual(str(error), "authorization_invalid")
            self.assertNotIn(TEST_SECRET, repr(error))
