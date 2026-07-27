from __future__ import annotations

import dataclasses
import importlib
import json
import logging
import re
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import channel_store, memory_action_ledger, memory_explicit_actions
from backend.tests._support import NoNetworkMixin
from backend.tests.test_memory_service import (
    bootstrap_runtime,
    memory_config,
)


class _Backend:
    def __init__(self):
        self.calls = []

    def _result(self, request, action_kind, category, memory_key):
        return memory_explicit_actions.ExplicitMemoryActionResult(
            request_id=request.request_id,
            action_kind=action_kind,
            status="completed",
            category=category,
            memory_key=memory_key,
            kind="project",
            scope_type="global_user",
            sensitivity="normal",
            replayed=False,
        )

    def remember(self, request, **projection):
        self.calls.append(("remember", projection))
        return self._result(request, "remember", "created", "M" * 32)

    def correct(self, request, **projection):
        self.calls.append(("correct", projection))
        return self._result(request, "correct", "corrected", "N" * 32)

    def forget(self, request, **projection):
        self.calls.append(("forget", projection))
        return self._result(request, "forget", "forgotten", request.memory_key)


class ExplicitMemoryActionContractTests(unittest.TestCase):
    def test_contracts_are_frozen_slotted_and_data_free(self):
        requests = (
            memory_explicit_actions.RememberExplicitMemoryRequest(
                "R" * 32,
                "project",
                "project",
                "private-scope",
                "private text",
                "normal",
            ),
            memory_explicit_actions.CorrectExplicitMemoryRequest(
                "C" * 32,
                "M" * 32,
                "replacement text",
                "normal",
            ),
            memory_explicit_actions.ForgetExplicitMemoryRequest(
                "F" * 32,
                "M" * 32,
            ),
        )
        for request in requests:
            with self.subTest(type=type(request).__name__):
                self.assertTrue(dataclasses.is_dataclass(request))
                self.assertFalse(hasattr(request, "__dict__"))
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    request.request_id = "X" * 32
                rendered = repr(request)
                self.assertNotIn("private", rendered)
                self.assertNotIn("M" * 32, rendered)

        result = memory_explicit_actions.ExplicitMemoryActionResult(
            "R" * 32,
            "remember",
            "completed",
            "created",
            "M" * 32,
            "project",
            "global_user",
            "normal",
            False,
        )
        self.assertFalse(hasattr(result, "__dict__"))
        self.assertNotIn("M" * 32, repr(result))

    def test_contracts_have_no_provenance_or_result_control_fields(self):
        for contract in (
            memory_explicit_actions.RememberExplicitMemoryRequest,
            memory_explicit_actions.CorrectExplicitMemoryRequest,
            memory_explicit_actions.ForgetExplicitMemoryRequest,
        ):
            names = {field.name for field in dataclasses.fields(contract)}
            self.assertTrue(names.isdisjoint({
                "origin",
                "channel",
                "source",
                "canonical_message_id",
                "result_category",
                "result_memory_key",
                "action_type",
            }))

    def test_origin_bound_facades_project_exact_server_values(self):
        factories = (
            (memory_explicit_actions.bind_operator_cli, "operator_cli", "web", "relay"),
            (memory_explicit_actions.bind_mcp, "mcp", "relay", "mcp"),
            (
                memory_explicit_actions.bind_telegram,
                "telegram",
                "telegram",
                "telegram",
            ),
            (
                memory_explicit_actions.bind_operit,
                "operit",
                "operit_share",
                "operit",
            ),
        )
        for factory, origin, channel, source in factories:
            with self.subTest(origin=origin):
                backend = _Backend()
                service = factory(backend)
                request = memory_explicit_actions.RememberExplicitMemoryRequest(
                    "R" * 32,
                    "project",
                    "global_user",
                    "",
                    "memory",
                    "normal",
                )
                service.remember_explicit_user_memory(request)
                self.assertEqual(backend.calls, [(
                    "remember",
                    {"origin": origin, "channel": channel, "source": source},
                )])
                self.assertEqual(repr(service), "<ExplicitMemoryActionService>")

    def test_fake_dict_object_and_subclass_requests_are_rejected(self):
        service = memory_explicit_actions.bind_operator_cli(_Backend())
        valid = memory_explicit_actions.RememberExplicitMemoryRequest(
            "R" * 32,
            "project",
            "global_user",
            "",
            "memory",
            "normal",
        )

        class Subclass(memory_explicit_actions.RememberExplicitMemoryRequest):
            pass

        values = (
            {"request_id": "R" * 32},
            object(),
            Subclass(
                valid.request_id,
                valid.kind,
                valid.scope_type,
                valid.scope_ref,
                valid.content,
                valid.sensitivity,
            ),
        )
        for value in values:
            with (
                self.subTest(type=type(value).__name__),
                self.assertRaisesRegex(
                    memory_explicit_actions.ExplicitMemoryActionError,
                    "invalid_request",
                ),
            ):
                service.remember_explicit_user_memory(value)

    def test_issue_request_id_reuses_server_side_ledger_factory(self):
        value = memory_explicit_actions.issue_request_id()
        self.assertRegex(value, r"[A-Za-z0-9_-]{32,96}\Z")


class ExplicitMemoryActionBackendTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "entry.sqlite3")
        with channel_store.connect(self.path) as connection:
            connection.execute(channel_store.RELAY_TABLE_DDL["messages"])
        channel_store.run_migrations(self.path)
        self._restart()

    def _restart(self):
        global memory_action_ledger, memory_explicit_actions
        runtime = bootstrap_runtime(self.path, memory_config())
        memory_action_ledger = importlib.import_module(
            "backend.memory_action_ledger"
        )
        memory_explicit_actions = importlib.import_module(
            "backend.memory_explicit_actions"
        )
        memory_explicit_actions = importlib.reload(memory_explicit_actions)
        backend = memory_explicit_actions.create_entry_backend(
            runtime.privileged_actions
        )
        self.service = memory_explicit_actions.bind_operator_cli(backend)

    def _remember(self, marker: str, content: str, *, kind: str = "project"):
        return self.service.remember_explicit_user_memory(
            memory_explicit_actions.RememberExplicitMemoryRequest(
                marker * 32,
                kind,
                "global_user",
                "",
                content,
                "normal",
            )
        )

    def _canonical_rows(self):
        with channel_store.connect(self.path) as connection:
            return connection.execute(
                "SELECT direction,kind,text,meta FROM messages ORDER BY id"
            ).fetchall()

    @staticmethod
    def _sql_fingerprint(statement: str) -> str:
        normalized = " ".join(statement.upper().split())
        normalized = re.sub(r"\s*,\s*", ",", normalized)
        normalized = re.sub(r"\s*=\s*", "=", normalized)
        normalized = re.sub(
            r"WHERE MEMORY_KEY='[A-Z0-9_-]+'",
            "WHERE MEMORY_KEY=?",
            normalized,
        )
        normalized = re.sub(
            r"WHERE ID=[0-9]+",
            "WHERE ID=?",
            normalized,
        )
        return normalized

    @classmethod
    def _forget_sql_fingerprints(cls) -> dict[str, str]:
        return {
            "A": cls._sql_fingerprint(
                """SELECT id,memory_key,kind,scope_type,scope_ref,status,
                          sensitivity,fingerprint_version,
                          normalized_fingerprint,superseded_by_id,updated_at
                   FROM memory_items WHERE memory_key=?"""
            ),
            "B_KEY": cls._sql_fingerprint(
                """SELECT id,memory_key,kind,scope_type,scope_ref,status,
                          sensitivity,fingerprint_version,
                          normalized_fingerprint,superseded_by_id,updated_at,
                          explicitness,confidence,first_observed_at,
                          last_confirmed_at,created_at
                   FROM memory_items WHERE memory_key=?"""
            ),
            "B_ID": cls._sql_fingerprint(
                """SELECT id,memory_key,kind,scope_type,scope_ref,status,
                          sensitivity,fingerprint_version,
                          normalized_fingerprint,superseded_by_id,updated_at,
                          explicitness,confidence,first_observed_at,
                          last_confirmed_at,created_at
                   FROM memory_items WHERE id=?"""
            ),
            "C": cls._sql_fingerprint(
                """SELECT id,memory_key,status,kind,scope_type,scope_ref,
                          sensitivity,fingerprint_version,explicitness,
                          confidence,updated_at,
                          normalized_content IS NULL AS content_absent,
                          normalized_fingerprint IS NULL AS fingerprint_absent,
                          superseded_by_id IS NULL AS supersession_absent
                   FROM memory_items WHERE memory_key=?"""
            ),
        }

    @classmethod
    def _memory_item_selects(cls, statements) -> tuple[str, ...]:
        result = []
        for statement in statements:
            fingerprint = cls._sql_fingerprint(statement)
            if (
                fingerprint.startswith("SELECT ")
                and re.search(
                    r"\b(?:FROM|JOIN)\s+(?:[A-Z0-9_]+\.)?MEMORY_ITEMS\b",
                    fingerprint,
                )
            ):
                result.append(fingerprint)
        return tuple(result)

    def _capture_forget_sql(self, call):
        store = self.service._backend._store
        store_module = importlib.import_module(type(store).__module__)
        ledger_module = store_module.memory_action_ledger
        store_channel_store = store_module.channel_store
        original_connect = store_channel_store.connect
        original_converter = store_module._forget_target_metadata
        original_execute = ledger_module._MemoryActionUnitOfWork._execute
        statements = []
        row_keys = []

        def traced_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        def capture_converter(row, **kwargs):
            row_keys.append(tuple(row.keys()))
            return original_converter(row, **kwargs)

        class CursorProxy:
            def __init__(self, cursor):
                self._cursor = cursor

            def fetchone(self):
                row = self._cursor.fetchone()
                if row is not None:
                    row_keys.append(tuple(row.keys()))
                return row

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        c_fingerprint = self._forget_sql_fingerprints()["C"]

        def capture_execute(uow, sql, parameters=()):
            cursor = original_execute(uow, sql, parameters)
            if self._sql_fingerprint(str(sql)) == c_fingerprint:
                return CursorProxy(cursor)
            return cursor

        with (
            mock.patch.object(
                store_channel_store,
                "connect",
                new=traced_connect,
            ),
            mock.patch.object(
                store_module,
                "_forget_target_metadata",
                new=capture_converter,
            ),
            mock.patch.object(
                ledger_module._MemoryActionUnitOfWork,
                "_execute",
                new=capture_execute,
            ),
            mock.patch.object(
                store_module.MemoryStore,
                "get_item_by_key",
                side_effect=AssertionError(
                    "forget must not call get_item_by_key"
                ),
            ),
        ):
            result = call()
        selects = self._memory_item_selects(statements)
        allowed = self._forget_sql_fingerprints()
        reverse = {value: name for name, value in allowed.items()}
        self.assertTrue(selects)
        self.assertTrue(all(value in reverse for value in selects), selects)
        return result, tuple(reverse[value] for value in selects), tuple(row_keys)

    def test_remember_created_replay_decision_and_suppression(self):
        created = self._remember("A", "Synthetic explicit entry memory")
        self.assertEqual(created.category, "created")
        canonical = self._canonical_rows()[0]
        self.assertEqual(
            (canonical["direction"], canonical["kind"]),
            ("in", "user"),
        )
        self.assertEqual(
            json.loads(canonical["meta"]),
            {"channel": "web", "source": "relay"},
        )
        replay = self._remember("A", "Synthetic explicit entry memory")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.memory_key, created.memory_key)
        self.assertEqual(len(self._canonical_rows()), 1)

        existing = self._remember("Y", "Synthetic explicit entry memory")
        self.assertEqual(existing.category, "idempotent_existing")
        self.assertEqual(existing.memory_key, created.memory_key)
        self.assertEqual(len(self._canonical_rows()), 2)

        decision = self._remember(
            "B",
            "Synthetic confirmed project decision",
            kind="decision",
        )
        self.assertEqual(decision.category, "created")
        with channel_store.connect(self.path) as connection:
            evidence = connection.execute(
                """SELECT action_type,evidence_type FROM memory_evidence_events
                   WHERE canonical_message_id=3"""
            ).fetchone()
        self.assertEqual(evidence["action_type"], "confirm_project_decision")
        self.assertEqual(evidence["evidence_type"], "confirmed_project_decision")

        forgotten = self.service.forget_explicit_user_memory(
            memory_explicit_actions.ForgetExplicitMemoryRequest(
                "C" * 32,
                created.memory_key,
            )
        )
        self.assertEqual(forgotten.category, "forgotten")
        suppressed = self._remember("D", "Synthetic explicit entry memory")
        self.assertEqual(suppressed.category, "suppressed")
        self.assertIsNone(suppressed.memory_key)

    def test_assistant_experience_and_fake_provenance_are_rejected(self):
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "unsupported_evidence",
        ):
            self._remember(
                "E",
                "Synthetic assistant experience",
                kind="assistant_experience",
            )
        backend = self.service._backend
        request = memory_explicit_actions.RememberExplicitMemoryRequest(
            "F" * 32,
            "project",
            "global_user",
            "",
            "Synthetic provenance attack",
            "normal",
        )
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "entry_composition_invalid",
        ):
            backend.remember(
                request,
                origin="operator_cli",
                channel="telegram",
                source="caller",
            )
        self.assertEqual(self._canonical_rows(), [])

    def test_correct_unchanged_corrected_and_forget_restart_replay(self):
        created = self._remember("G", "Synthetic original memory")
        unchanged_request = memory_explicit_actions.CorrectExplicitMemoryRequest(
            "H" * 32,
            created.memory_key,
            "Synthetic original memory",
            "normal",
        )
        unchanged = self.service.correct_explicit_user_memory(unchanged_request)
        self.assertEqual(unchanged.category, "unchanged")

        corrected_request = memory_explicit_actions.CorrectExplicitMemoryRequest(
            "I" * 32,
            created.memory_key,
            "Synthetic replacement memory",
            "normal",
        )
        corrected = self.service.correct_explicit_user_memory(corrected_request)
        self.assertEqual(corrected.category, "corrected")
        self.assertNotEqual(corrected.memory_key, created.memory_key)

        forget_request = memory_explicit_actions.ForgetExplicitMemoryRequest(
            "J" * 32,
            corrected.memory_key,
        )
        forgotten = self.service.forget_explicit_user_memory(forget_request)
        self.assertEqual(forgotten.category, "forgotten")
        canonical = self._canonical_rows()[-1]
        self.assertEqual(
            canonical["text"],
            f"Forget explicit memory: {corrected.memory_key}",
        )
        self.assertNotIn("Synthetic replacement memory", canonical["text"])

        self._restart()
        replay_request = memory_explicit_actions.ForgetExplicitMemoryRequest(
            "J" * 32,
            corrected.memory_key,
        )
        replay = self.service.forget_explicit_user_memory(replay_request)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.category, "forgotten")
        self.assertEqual(len(self._canonical_rows()), 4)

        second = self.service.forget_explicit_user_memory(
            memory_explicit_actions.ForgetExplicitMemoryRequest(
                "K" * 32,
                corrected.memory_key,
            )
        )
        self.assertEqual(second.category, "already_forgotten")
        canonical_count = len(self._canonical_rows())
        self._restart()
        second_replay = self.service.forget_explicit_user_memory(
            memory_explicit_actions.ForgetExplicitMemoryRequest(
                "K" * 32,
                corrected.memory_key,
            )
        )
        self.assertTrue(second_replay.replayed)
        self.assertEqual(second_replay.category, "already_forgotten")
        self.assertEqual(len(self._canonical_rows()), canonical_count)

    def test_forget_memory_item_sql_is_exact_a_b_c_for_all_entry_paths(self):
        target = self._remember("5", "Synthetic SQL allowlist target")
        request = memory_explicit_actions.ForgetExplicitMemoryRequest(
            "6" * 32,
            target.memory_key,
        )
        result, sequence, row_keys = self._capture_forget_sql(
            lambda: self.service.forget_explicit_user_memory(request)
        )
        self.assertEqual(result.category, "forgotten")
        self.assertEqual(sequence, ("A", "B_KEY", "B_ID", "C"))

        a_keys = (
            "id",
            "memory_key",
            "kind",
            "scope_type",
            "scope_ref",
            "status",
            "sensitivity",
            "fingerprint_version",
            "normalized_fingerprint",
            "superseded_by_id",
            "updated_at",
        )
        b_keys = a_keys + (
            "explicitness",
            "confidence",
            "first_observed_at",
            "last_confirmed_at",
            "created_at",
        )
        c_keys = (
            "id",
            "memory_key",
            "status",
            "kind",
            "scope_type",
            "scope_ref",
            "sensitivity",
            "fingerprint_version",
            "explicitness",
            "confidence",
            "updated_at",
            "content_absent",
            "fingerprint_absent",
            "supersession_absent",
        )
        self.assertEqual(row_keys, (a_keys, b_keys, b_keys, c_keys))
        self.assertTrue(all(
            "normalized_content" not in keys for keys in row_keys[:-1]
        ))
        self.assertEqual(row_keys[-1], c_keys)

        replay, sequence, _row_keys = self._capture_forget_sql(
            lambda: self.service.forget_explicit_user_memory(request)
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(sequence, ("A", "C"))

        self._restart()
        request = memory_explicit_actions.ForgetExplicitMemoryRequest(
            "6" * 32,
            target.memory_key,
        )
        restarted, sequence, _row_keys = self._capture_forget_sql(
            lambda: self.service.forget_explicit_user_memory(request)
        )
        self.assertTrue(restarted.replayed)
        self.assertEqual(sequence, ("A", "C"))

        already_request = memory_explicit_actions.ForgetExplicitMemoryRequest(
            "7" * 32,
            target.memory_key,
        )
        already, sequence, _row_keys = self._capture_forget_sql(
            lambda: self.service.forget_explicit_user_memory(
                already_request
            )
        )
        self.assertEqual(already.category, "already_forgotten")
        self.assertEqual(sequence, ("A", "B_KEY", "C"))

        uncertain_target = self._remember(
            "8",
            "Synthetic uncertain SQL target",
        )
        uncertain_request = (
            memory_explicit_actions.ForgetExplicitMemoryRequest(
                "9" * 32,
                uncertain_target.memory_key,
            )
        )
        ledger_module = importlib.import_module(
            type(self.service._backend._store._action_unit_of_work()).__module__
        )
        original_commit = ledger_module._MemoryActionUnitOfWork.commit
        calls = 0

        def committed_then_uncertain(uow):
            nonlocal calls
            calls += 1
            value = original_commit(uow)
            if calls == 1:
                raise ledger_module.MemoryActionLedgerError(
                    "transaction_outcome_uncertain"
                )
            return value

        with mock.patch.object(
            ledger_module._MemoryActionUnitOfWork,
            "commit",
            new=committed_then_uncertain,
        ):
            uncertain, sequence, _row_keys = self._capture_forget_sql(
                lambda: self.service.forget_explicit_user_memory(
                    uncertain_request
                )
            )
        self.assertTrue(uncertain.replayed)
        self.assertEqual(
            sequence,
            ("A", "B_KEY", "B_ID", "C", "A", "C"),
        )

    def test_forget_never_materializes_old_plaintext_in_python_results(self):
        sentinel = "FORGET_OLD_CONTENT_SENTINEL_7f3149e62a"
        created = self._remember("s", sentinel)
        request = memory_explicit_actions.ForgetExplicitMemoryRequest(
            "t" * 32,
            created.memory_key,
        )
        store_module = importlib.import_module(
            type(self.service._backend._store).__module__
        )
        ledger_module = store_module.memory_action_ledger
        store_channel_store = store_module.channel_store
        original_connect = store_channel_store.connect
        original_metadata = store_module.MemoryStore._get_forget_target_metadata
        original_record = (
            ledger_module._MemoryActionUnitOfWork._record_store_outcome
        )
        original_snapshot = (
            ledger_module._MemoryActionUnitOfWork
            ._build_terminal_semantic_snapshot
        )
        statements = []
        metadata_values = []
        store_items = []
        snapshots = []
        log_messages = []

        def traced_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        def capture_metadata(store, *args, **kwargs):
            value = original_metadata(store, *args, **kwargs)
            metadata_values.append(value)
            return value

        def capture_record(uow, **kwargs):
            item = kwargs["store_result"].item
            store_items.append(dict(item) if isinstance(item, dict) else item)
            return original_record(uow, **kwargs)

        def capture_snapshot(uow, *args, **kwargs):
            value = original_snapshot(uow, *args, **kwargs)
            snapshots.append(value)
            return value

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                log_messages.append(record.getMessage())

        handler = CaptureHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        self.addCleanup(root_logger.removeHandler, handler)
        with (
            mock.patch.object(
                store_channel_store,
                "connect",
                new=traced_connect,
            ),
            mock.patch.object(
                store_module.MemoryStore,
                "get_item_by_key",
                side_effect=AssertionError(
                    "forget must not call get_item_by_key"
                ),
            ),
            mock.patch.object(
                store_module.MemoryStore,
                "_get_forget_target_metadata",
                new=capture_metadata,
            ),
            mock.patch.object(
                ledger_module._MemoryActionUnitOfWork,
                "_record_store_outcome",
                new=capture_record,
            ),
            mock.patch.object(
                ledger_module._MemoryActionUnitOfWork,
                "_build_terminal_semantic_snapshot",
                new=capture_snapshot,
            ),
        ):
            result = self.service.forget_explicit_user_memory(request)

        self.assertEqual(result.category, "forgotten")
        self.assertTrue(metadata_values)
        self.assertTrue(all(
            type(value) is store_module._ForgetTargetMetadataV1
            for value in metadata_values
        ))
        self.assertEqual(
            tuple(
                field.name
                for field in dataclasses.fields(
                    store_module._ForgetTargetMetadataV1
                )
            ),
            (
                "memory_id",
                "memory_key",
                "kind",
                "scope_type",
                "scope_ref",
                "status",
                "sensitivity",
                "fingerprint_version",
                "normalized_fingerprint",
                "superseded_by_id",
                "updated_at",
            ),
        )
        self.assertTrue(store_items)
        self.assertTrue(all(
            item["normalized_content"] is None for item in store_items
        ))
        self.assertTrue(snapshots)

        selects = tuple(
            " ".join(statement.upper().split())
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        )
        memory_item_selects = tuple(
            statement
            for statement in selects
            if "FROM MEMORY_ITEMS" in statement
        )
        self.assertTrue(memory_item_selects)
        self.assertFalse(any(
            "SELECT * FROM MEMORY_ITEMS" in statement
            or "SELECT I.*" in statement
            for statement in memory_item_selects
        ))
        for statement in memory_item_selects:
            without_absence_flag = statement.replace(
                "I.NORMALIZED_CONTENT IS NULL AS CONTENT_ABSENT",
                "",
            ).replace(
                "NORMALIZED_CONTENT IS NULL AS CONTENT_ABSENT",
                "",
            )
            self.assertNotIn(
                "NORMALIZED_CONTENT",
                without_absence_flag,
            )
            if "CONTENT_ABSENT" in statement:
                self.assertIn(
                    "NORMALIZED_CONTENT IS NULL AS CONTENT_ABSENT",
                    statement,
                )
                self.assertIn(
                    "NORMALIZED_FINGERPRINT IS NULL AS FINGERPRINT_ABSENT",
                    statement,
                )
                self.assertIn(
                    "SUPERSEDED_BY_ID IS NULL AS SUPERSESSION_ABSENT",
                    statement,
                )

        forget_canonical = self._canonical_rows()[-1]
        with channel_store.connect(self.path) as connection:
            terminal = dict(connection.execute(
                """SELECT request_id,action_kind,origin,target_memory_key,
                          result_memory_key,status,result_category
                   FROM memory_action_requests WHERE request_id=?""",
                (request.request_id,),
            ).fetchone())
            tombstone = dict(connection.execute(
                """SELECT memory_key,status,normalized_content,
                          normalized_fingerprint
                   FROM memory_items WHERE memory_key=?""",
                (created.memory_key,),
            ).fetchone())
        observed = (
            statements,
            metadata_values,
            store_items,
            tuple(dataclasses.asdict(value) for value in snapshots),
            result,
            repr(result),
            repr(request),
            repr(self.service),
            repr(self.service._backend),
            forget_canonical["text"],
            forget_canonical["meta"],
            terminal,
            tombstone,
            log_messages,
        )
        self.assertNotIn(sentinel, repr(observed))
        self.assertEqual(tombstone["status"], "forgotten")
        self.assertIsNone(tombstone["normalized_content"])
        self.assertIsNone(tombstone["normalized_fingerprint"])

    def test_request_binding_conflicts_across_payload_and_action(self):
        created = self._remember("L", "Synthetic binding memory")
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "request_binding_conflict",
        ):
            self._remember("L", "Synthetic changed binding")
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "request_binding_conflict",
        ):
            self.service.forget_explicit_user_memory(
                memory_explicit_actions.ForgetExplicitMemoryRequest(
                    "L" * 32,
                    created.memory_key,
                )
            )

    def test_unknown_and_nonactive_correct_targets_are_data_free(self):
        request = memory_explicit_actions.CorrectExplicitMemoryRequest(
            "M" * 32,
            "Z" * 32,
            "Synthetic replacement",
            "normal",
        )
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "not_found",
        ):
            self.service.correct_explicit_user_memory(request)
        self.assertEqual(self._canonical_rows(), [])

    def test_forget_metadata_rejects_fake_subclass_and_invalid_fingerprints(self):
        created = self._remember("u", "Synthetic forget metadata target")
        store_module = importlib.import_module(
            type(self.service._backend._store).__module__
        )
        with channel_store.connect(self.path) as connection:
            row = connection.execute(
                """SELECT id,memory_key,kind,scope_type,scope_ref,status,
                          sensitivity,fingerprint_version,
                          normalized_fingerprint,superseded_by_id,updated_at
                   FROM memory_items WHERE memory_key=?""",
                (created.memory_key,),
            ).fetchone()
        native = store_module._ForgetTargetMetadataV1(
            memory_id=row["id"],
            memory_key=row["memory_key"],
            kind=row["kind"],
            scope_type=row["scope_type"],
            scope_ref=row["scope_ref"],
            status=row["status"],
            sensitivity=row["sensitivity"],
            fingerprint_version=row["fingerprint_version"],
            normalized_fingerprint=row["normalized_fingerprint"],
            superseded_by_id=row["superseded_by_id"],
            updated_at=row["updated_at"],
        )
        self.assertNotIn(native.memory_key, repr(native))
        self.assertNotIn(native.normalized_fingerprint.hex(), repr(native))

        class MetadataSubclass(store_module._ForgetTargetMetadataV1):
            pass

        fake_values = (
            dataclasses.asdict(native),
            SimpleNamespace(**dataclasses.asdict(native)),
            MetadataSubclass(
                native.memory_id,
                native.memory_key,
                native.kind,
                native.scope_type,
                native.scope_ref,
                native.status,
                native.sensitivity,
                native.fingerprint_version,
                native.normalized_fingerprint,
                native.superseded_by_id,
                native.updated_at,
            ),
            dataclasses.replace(native),
        )
        before = len(self._canonical_rows())
        for index, fake in enumerate(fake_values):
            with (
                self.subTest(type=type(fake).__name__),
                mock.patch.object(
                    store_module.MemoryStore,
                    "_get_forget_target_metadata",
                    return_value=fake,
                ),
                self.assertRaisesRegex(
                    memory_explicit_actions.ExplicitMemoryActionError,
                    "invalid_state|request_binding_conflict",
                ),
            ):
                self.service.forget_explicit_user_memory(
                    memory_explicit_actions.ForgetExplicitMemoryRequest(
                        chr(118 + index) * 32,
                        created.memory_key,
                    )
                )
        self.assertEqual(len(self._canonical_rows()), before)

        with channel_store.connect(self.path) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                """UPDATE memory_items SET normalized_fingerprint=x'01'
                   WHERE memory_key=?""",
                (created.memory_key,),
            )
            connection.execute("PRAGMA ignore_check_constraints=OFF")
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "invalid_state",
        ):
            self.service.forget_explicit_user_memory(
                memory_explicit_actions.ForgetExplicitMemoryRequest(
                    "x" * 32,
                    created.memory_key,
                )
            )
        self.assertEqual(len(self._canonical_rows()), before)
        with channel_store.connect(self.path) as connection:
            connection.execute(
                """UPDATE memory_items SET normalized_fingerprint=?
                   WHERE memory_key=?""",
                (native.normalized_fingerprint, created.memory_key),
            )

        forgotten_target = self._remember(
            "y",
            "Synthetic forgotten fingerprint target",
        )
        self.service.forget_explicit_user_memory(
            memory_explicit_actions.ForgetExplicitMemoryRequest(
                "z" * 32,
                forgotten_target.memory_key,
            )
        )
        after_forget = len(self._canonical_rows())
        with channel_store.connect(self.path) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                """UPDATE memory_items SET normalized_fingerprint=zeroblob(32)
                   WHERE memory_key=?""",
                (forgotten_target.memory_key,),
            )
            connection.execute("PRAGMA ignore_check_constraints=OFF")
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "invalid_state",
        ):
            self.service.forget_explicit_user_memory(
                memory_explicit_actions.ForgetExplicitMemoryRequest(
                    "0" * 32,
                    forgotten_target.memory_key,
                )
            )
        self.assertEqual(len(self._canonical_rows()), after_forget)

    def test_forget_registration_detects_every_metadata_field_mutation(self):
        created = self._remember("Q", "Synthetic ownership mutation target")
        store = self.service._backend._store
        store_type = type(store)
        store_module = importlib.import_module(store_type.__module__)
        original_get = store_type._get_forget_target_metadata

        def counts():
            with channel_store.connect(self.path) as connection:
                return {
                    table: int(connection.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0])
                    for table in (
                        "messages",
                        "memory_action_requests",
                        "memory_evidence_events",
                        "memory_items",
                        "memory_sources",
                        "memory_suppressions",
                    )
                }

        mutations = (
            ("memory_id", lambda value: value.memory_id + 1),
            ("memory_key", lambda _value: "Z" * 32),
            ("kind", lambda _value: "decision"),
            ("scope_type", lambda _value: "project"),
            ("scope_ref", lambda _value: "synthetic"),
            ("status", lambda _value: "forgotten"),
            ("sensitivity", lambda _value: "sensitive"),
            (
                "fingerprint_version",
                lambda value: value.fingerprint_version + 1,
            ),
            ("normalized_fingerprint", lambda _value: b"x" * 32),
            ("superseded_by_id", lambda value: value.memory_id),
            ("updated_at", lambda _value: "2030-01-02T03:04:05+00:00"),
        )
        before = counts()
        for index, (field_name, replacement) in enumerate(mutations):
            def mutate_after_registration(
                current_store,
                *args,
                _field_name=field_name,
                _replacement=replacement,
                **kwargs,
            ):
                metadata = original_get(current_store, *args, **kwargs)
                object.__setattr__(
                    metadata,
                    _field_name,
                    _replacement(metadata),
                )
                return metadata

            with (
                self.subTest(field=field_name),
                mock.patch.object(
                    store_type,
                    "_get_forget_target_metadata",
                    new=mutate_after_registration,
                ),
                self.assertRaisesRegex(
                    memory_explicit_actions.ExplicitMemoryActionError,
                    "invalid_state",
                ),
            ):
                self.service.forget_explicit_user_memory(
                    memory_explicit_actions.ForgetExplicitMemoryRequest(
                        chr(65 + index) * 32,
                        created.memory_key,
                    )
                )
            self.assertEqual(counts(), before)

        for index, (field_name, replacement) in enumerate(mutations):
            def replace_after_registration(
                current_store,
                *args,
                _field_name=field_name,
                _replacement=replacement,
                **kwargs,
            ):
                metadata = original_get(current_store, *args, **kwargs)
                return dataclasses.replace(
                    metadata,
                    **{_field_name: _replacement(metadata)},
                )

            with (
                self.subTest(replaced_field=field_name),
                mock.patch.object(
                    store_type,
                    "_get_forget_target_metadata",
                    new=replace_after_registration,
                ),
                self.assertRaisesRegex(
                    memory_explicit_actions.ExplicitMemoryActionError,
                    "invalid_state",
                ),
            ):
                self.service.forget_explicit_user_memory(
                    memory_explicit_actions.ForgetExplicitMemoryRequest(
                        chr(77 + index) * 32,
                        created.memory_key,
                    )
                )
            self.assertEqual(counts(), before)

        def register_twice(current_store, *args, **kwargs):
            metadata = original_get(current_store, *args, **kwargs)
            original_get(current_store, *args, **kwargs)
            return metadata

        with (
            mock.patch.object(
                store_type,
                "_get_forget_target_metadata",
                new=register_twice,
            ),
            self.assertRaisesRegex(
                memory_explicit_actions.ExplicitMemoryActionError,
                "invalid_state",
            ),
        ):
            self.service.forget_explicit_user_memory(
                memory_explicit_actions.ForgetExplicitMemoryRequest(
                    "L" * 32,
                    created.memory_key,
                )
            )
        self.assertEqual(counts(), before)

        with self.assertRaisesRegex(
            store_module.MemoryStoreError,
            "transaction_context_invalid",
        ):
            store.forget_memory_atomic(
                memory_key=created.memory_key,
                sources=(),
                authorization=None,
            )
        self.assertEqual(counts(), before)

    def test_forget_registration_rejects_pre_reload_metadata_instance(self):
        created = self._remember("R", "Synthetic pre-reload metadata target")
        store = self.service._backend._store
        old_store_type = type(store)
        old_metadata = store._get_forget_target_metadata(created.memory_key)
        store_module = importlib.import_module(old_store_type.__module__)
        importlib.reload(store_module)
        before = len(self._canonical_rows())
        try:
            with (
                mock.patch.object(
                    old_store_type,
                    "_get_forget_target_metadata",
                    return_value=old_metadata,
                ),
                self.assertRaisesRegex(
                    memory_explicit_actions.ExplicitMemoryActionError,
                    "invalid_state",
                ),
            ):
                self.service.forget_explicit_user_memory(
                    memory_explicit_actions.ForgetExplicitMemoryRequest(
                        "S" * 32,
                        created.memory_key,
                    )
                )
        finally:
            self._restart()
        self.assertEqual(len(self._canonical_rows()), before)

    def test_forget_metadata_faults_and_midflow_change_roll_back_all_growth(self):
        target = self._remember("1", "Synthetic metadata fault target")
        store_module = importlib.import_module(
            type(self.service._backend._store).__module__
        )
        ledger_module = importlib.import_module(
            "backend.memory_action_ledger"
        )

        def counts():
            with channel_store.connect(self.path) as connection:
                return {
                    table: connection.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0]
                    for table in (
                        "messages",
                        "memory_action_requests",
                        "memory_evidence_events",
                        "memory_items",
                        "memory_sources",
                        "memory_suppressions",
                    )
                }

        missing_before = counts()
        with self.assertRaisesRegex(
            memory_explicit_actions.ExplicitMemoryActionError,
            "not_found",
        ):
            self.service.forget_explicit_user_memory(
                memory_explicit_actions.ForgetExplicitMemoryRequest(
                    "4" * 32,
                    "Z" * 32,
                )
            )
        self.assertEqual(counts(), missing_before)

        before = counts()
        original_execute = ledger_module._MemoryActionUnitOfWork._execute

        def fail_metadata_query(uow, sql, parameters=()):
            normalized = " ".join(str(sql).upper().split())
            if (
                "FROM MEMORY_ITEMS WHERE MEMORY_KEY=?" in normalized
                and "NORMALIZED_FINGERPRINT" in normalized
                and "NORMALIZED_CONTENT" not in normalized
            ):
                raise sqlite3.OperationalError("synthetic metadata failure")
            return original_execute(uow, sql, parameters)

        with (
            mock.patch.object(
                ledger_module._MemoryActionUnitOfWork,
                "_execute",
                new=fail_metadata_query,
            ),
            self.assertRaisesRegex(
                memory_explicit_actions.ExplicitMemoryActionError,
                "storage_unavailable",
            ),
        ):
            self.service.forget_explicit_user_memory(
                memory_explicit_actions.ForgetExplicitMemoryRequest(
                    "2" * 32,
                    target.memory_key,
                )
            )
        self.assertEqual(counts(), before)

        original_require = (
            ledger_module._MemoryActionUnitOfWork
            ._require_registered_forget_target
        )
        calls = 0

        def changed_metadata(uow, *, store):
            nonlocal calls
            calls += 1
            registration = uow._forget_target_registration
            self.assertIsNotNone(registration)
            object.__setattr__(
                registration._metadata,
                "kind",
                "decision",
            )
            return original_require(uow, store=store)

        with (
            mock.patch.object(
                ledger_module._MemoryActionUnitOfWork,
                "_require_registered_forget_target",
                new=changed_metadata,
            ),
            self.assertRaisesRegex(
                memory_explicit_actions.ExplicitMemoryActionError,
                "request_binding_conflict",
            ),
        ):
            self.service.forget_explicit_user_memory(
                memory_explicit_actions.ForgetExplicitMemoryRequest(
                    "3" * 32,
                    target.memory_key,
                )
            )
        self.assertEqual(calls, 1)
        self.assertEqual(counts(), before)

    def test_correct_suppression_and_nonactive_targets_fail_closed(self):
        target = self._remember("a", "Synthetic correction target")
        blocked = self._remember("b", "Synthetic blocked replacement")
        self.service.forget_explicit_user_memory(
            memory_explicit_actions.ForgetExplicitMemoryRequest(
                "c" * 32,
                blocked.memory_key,
            )
        )
        suppressed = self.service.correct_explicit_user_memory(
            memory_explicit_actions.CorrectExplicitMemoryRequest(
                "d" * 32,
                target.memory_key,
                "Synthetic blocked replacement",
                "normal",
            )
        )
        self.assertEqual(suppressed.category, "suppressed")
        self.assertIsNone(suppressed.memory_key)

        corrected = self.service.correct_explicit_user_memory(
            memory_explicit_actions.CorrectExplicitMemoryRequest(
                "e" * 32,
                target.memory_key,
                "Synthetic active replacement",
                "normal",
            )
        )
        for marker, memory_key in (
            ("f", target.memory_key),
            ("g", blocked.memory_key),
        ):
            with (
                self.subTest(marker=marker),
                self.assertRaisesRegex(
                    memory_explicit_actions.ExplicitMemoryActionError,
                    "invalid_state",
                ),
            ):
                self.service.correct_explicit_user_memory(
                    memory_explicit_actions.CorrectExplicitMemoryRequest(
                        marker * 32,
                        memory_key,
                        "Synthetic rejected replacement",
                        "normal",
                    )
                )
        self.assertNotEqual(corrected.memory_key, target.memory_key)

    def test_policy_rejects_sensitive_storage_and_encoded_credentials(self):
        cases = (
            (
                "h",
                "A synthetic private preference",
                "sensitive",
                "sensitive_storage_disabled",
            ),
            (
                "i",
                "I was diagnosed with a synthetic condition",
                "normal",
                "sensitivity_downgrade",
            ),
            (
                "j",
                "%3Ftoken%3Dsynthetic-secret-value-12345",
                "normal",
                "secret_detected",
            ),
            (
                "k",
                r'{"\u0061pi_key":"synthetic-secret-value-12345"}',
                "normal",
                "secret_detected",
            ),
        )
        for marker, content, sensitivity, category in cases:
            with self.subTest(category=category):
                request = memory_explicit_actions.RememberExplicitMemoryRequest(
                    marker * 32,
                    "project",
                    "global_user",
                    "",
                    content,
                    sensitivity,
                )
                with self.assertRaises(
                    memory_explicit_actions.ExplicitMemoryActionError
                ) as raised:
                    self.service.remember_explicit_user_memory(request)
                self.assertEqual(raised.exception.category, category)
                self.assertNotIn(content, str(raised.exception))
                self.assertNotIn(content, repr(request))
        self.assertEqual(self._canonical_rows(), [])

    def test_ordinary_canonical_row_is_never_reused(self):
        with channel_store.connect(self.path) as connection:
            ordinary_id = connection.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES('now','in','user','Synthetic ordinary history',
                          '{"channel":"web","source":"relay"}')"""
            ).lastrowid
        created = self._remember("l", "Synthetic new explicit action")
        with channel_store.connect(self.path) as connection:
            evidence_id = connection.execute(
                """SELECT canonical_message_id FROM memory_evidence_events
                   WHERE action_type='remember_explicit_user'"""
            ).fetchone()[0]
        self.assertNotEqual(evidence_id, ordinary_id)
        self.assertEqual(created.category, "created")
        self.assertEqual(len(self._canonical_rows()), 2)

    def test_concurrent_same_request_has_one_canonical_for_2_4_8_callers(self):
        for workers in (2, 4, 8):
            with self.subTest(workers=workers):
                marker = chr(78 + workers)
                request = memory_explicit_actions.RememberExplicitMemoryRequest(
                    marker * 32,
                    "project",
                    "global_user",
                    "",
                    f"Synthetic concurrent memory {workers}",
                    "normal",
                )
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    results = list(pool.map(
                        lambda _index: self.service.remember_explicit_user_memory(
                            request
                        ),
                        range(workers),
                    ))
                self.assertEqual(
                    {result.memory_key for result in results},
                    {results[0].memory_key},
                )
                self.assertEqual(
                    sum(not result.replayed for result in results),
                    1,
                )
        with channel_store.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM memory_action_requests"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM messages").fetchone()[0],
                3,
            )

    def test_concurrent_different_requests_same_content_are_all_terminal(self):
        requests = tuple(
            memory_explicit_actions.RememberExplicitMemoryRequest(
                chr(65 + index) * 32,
                "project",
                "global_user",
                "",
                "Synthetic shared concurrent content",
                "normal",
            )
            for index in range(4)
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                self.service.remember_explicit_user_memory,
                requests,
            ))
        self.assertEqual(
            sorted(result.category for result in results),
            ["created", "idempotent_existing", "idempotent_existing",
             "idempotent_existing"],
        )
        self.assertEqual(len({result.memory_key for result in results}), 1)
        self.assertEqual(len(self._canonical_rows()), 4)

    def test_concurrent_correct_correct_and_correct_forget_serialize(self):
        for case in ("correct_correct", "correct_forget"):
            with self.subTest(case=case):
                target = self._remember(
                    "m" if case == "correct_correct" else "n",
                    f"Synthetic {case} target",
                )
                first = lambda: self.service.correct_explicit_user_memory(
                    memory_explicit_actions.CorrectExplicitMemoryRequest(
                        ("o" if case == "correct_correct" else "p") * 32,
                        target.memory_key,
                        f"Synthetic {case} replacement one",
                        "normal",
                    )
                )
                if case == "correct_correct":
                    second = lambda: self.service.correct_explicit_user_memory(
                        memory_explicit_actions.CorrectExplicitMemoryRequest(
                            "q" * 32,
                            target.memory_key,
                            "Synthetic second correction",
                            "normal",
                        )
                    )
                else:
                    second = lambda: self.service.forget_explicit_user_memory(
                        memory_explicit_actions.ForgetExplicitMemoryRequest(
                            "r" * 32,
                            target.memory_key,
                        )
                    )
                calls = (first, second)

                def run(call):
                    try:
                        return call()
                    except memory_explicit_actions.ExplicitMemoryActionError as error:
                        return error

                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(run, calls))
                successes = [
                    value for value in outcomes
                    if isinstance(
                        value,
                        memory_explicit_actions.ExplicitMemoryActionResult,
                    )
                ]
                failures = [
                    value for value in outcomes
                    if isinstance(
                        value,
                        memory_explicit_actions.ExplicitMemoryActionError,
                    )
                ]
                self.assertEqual(len(successes), 1)
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0].category, "invalid_state")

    def test_uncertain_commit_queries_terminal_without_reexecuting(self):
        original = memory_action_ledger._MemoryActionUnitOfWork.commit
        calls = 0

        def committed_then_uncertain(uow):
            nonlocal calls
            calls += 1
            result = original(uow)
            if calls == 1:
                raise memory_action_ledger.MemoryActionLedgerError(
                    "transaction_outcome_uncertain"
                )
            return result

        request = memory_explicit_actions.RememberExplicitMemoryRequest(
            "W" * 32,
            "project",
            "global_user",
            "",
            "Synthetic uncertain commit memory",
            "normal",
        )
        with mock.patch.object(
            memory_action_ledger._MemoryActionUnitOfWork,
            "commit",
            new=committed_then_uncertain,
        ):
            result = self.service.remember_explicit_user_memory(request)
        self.assertTrue(result.replayed)
        self.assertEqual(calls, 2)
        self.assertEqual(len(self._canonical_rows()), 1)

    def test_uncertain_commit_without_terminal_never_blindly_reexecutes(self):
        original = memory_action_ledger._MemoryActionUnitOfWork.commit
        calls = 0

        def rolled_back_then_uncertain(uow):
            nonlocal calls
            calls += 1
            if calls == 1:
                uow.rollback()
                raise memory_action_ledger.MemoryActionLedgerError(
                    "transaction_outcome_uncertain"
                )
            return original(uow)

        request = memory_explicit_actions.RememberExplicitMemoryRequest(
            "X" * 32,
            "project",
            "global_user",
            "",
            "Synthetic absent uncertain terminal",
            "normal",
        )
        with (
            mock.patch.object(
                memory_action_ledger._MemoryActionUnitOfWork,
                "commit",
                new=rolled_back_then_uncertain,
            ),
            self.assertRaisesRegex(
                memory_explicit_actions.ExplicitMemoryActionError,
                "transaction_outcome_uncertain",
            ),
        ):
            self.service.remember_explicit_user_memory(request)
        self.assertEqual(calls, 1)
        self.assertEqual(self._canonical_rows(), [])


if __name__ == "__main__":
    unittest.main()
