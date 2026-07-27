from __future__ import annotations

import dataclasses
import importlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
        global memory_explicit_actions
        runtime = bootstrap_runtime(self.path, memory_config())
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

    def test_remember_created_replay_decision_and_suppression(self):
        created = self._remember("A", "Synthetic explicit entry memory")
        self.assertEqual(created.category, "created")
        replay = self._remember("A", "Synthetic explicit entry memory")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.memory_key, created.memory_key)
        self.assertEqual(len(self._canonical_rows()), 1)

        decision = self._remember(
            "B",
            "Synthetic confirmed project decision",
            kind="decision",
        )
        self.assertEqual(decision.category, "created")
        with channel_store.connect(self.path) as connection:
            evidence = connection.execute(
                """SELECT action_type,evidence_type FROM memory_evidence_events
                   WHERE canonical_message_id=2"""
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
