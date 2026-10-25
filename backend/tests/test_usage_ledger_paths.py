"""Synthetic paths/metadata only; no source DB content, network or live settings."""
import copy
import os
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch

from backend.usage_ledger import LedgerError
from backend.usage_ledger_composition import LedgerServices
from backend.usage_ledger_paths import SIDECARS, lexically_separate, separate_paths
from backend.usage_ledger_preflight import mock_plan, review_plan


class LedgerPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ledger, self.relay, self.native = (self.root/name for name in ('ledger.sqlite','relay.sqlite','native.sqlite'))

    def check(self):
        with patch('sqlite3.connect',side_effect=AssertionError('metadata must not open SQLite')):
            return separate_paths(self.ledger,self.relay,self.native)

    def rejected_before_sqlite(self, ledger=None, relay=None, native=None):
        with patch('sqlite3.connect',side_effect=AssertionError('reject before ledger initialization')):
            with self.assertRaisesRegex(LedgerError,'^ledger_composition_invalid$'):
                LedgerServices(ledger_path=ledger or self.ledger,relay_db=relay or self.relay,
                               native_db=native or self.native,continuation_key=b'synthetic-test-key-only-123456789')

    def test_lexical_checks_are_pure_and_include_sidecars_in_both_directions(self):
        with patch.object(Path,'stat',side_effect=AssertionError('no metadata')):
            for suffix in SIDECARS:
                a = PurePosixPath('/var/data/a'); b = PurePosixPath(str(a)+suffix)
                self.assertFalse(lexically_separate([a,b]))
                self.assertFalse(lexically_separate([b,a]))
            self.assertFalse(lexically_separate([PurePosixPath('/var/data/a'),PurePosixPath('/var/data/a-wal/nested')]))
            self.assertTrue(lexically_separate([PurePosixPath('/var/data/a'),PurePosixPath('/var/data/b')]))

    def test_preflight_rejects_reserved_sidecar_paths_for_every_database_role(self):
        base = mock_plan()
        for first,second in (('ledger_db','relay_db'),('relay_db','ledger_db'),
                             ('ledger_db','codex_db'),('codex_db','ledger_db'),('relay_db','codex_db')):
            for suffix in SIDECARS:
                plan=copy.deepcopy(base);plan['storage'][second]=plan['storage'][first]+suffix
                before=copy.deepcopy(plan)
                result=review_plan(plan)
                self.assertFalse(result['configuration_consistent'])
                self.assertEqual(result['checks']['ledger_separate_from_source_databases'],'missing_or_conflicting')
                self.assertFalse(result['production_verified']);self.assertEqual(plan,before)

    def test_missing_independent_files_are_checked_without_creating_anything(self):
        self.assertEqual(self.check(),(self.ledger,self.relay,self.native))
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertEqual(separate_paths(self.ledger,self.relay,None),(self.ledger,self.relay,None))

    def test_direct_main_to_sidecar_collisions_reject_before_ledger_creation(self):
        for suffix in SIDECARS:
            self.rejected_before_sqlite(ledger=Path(str(self.relay)+suffix))
            self.rejected_before_sqlite(relay=Path(str(self.ledger)+suffix))
            self.rejected_before_sqlite(native=Path(str(self.ledger)+suffix))
        self.assertEqual(list(self.root.iterdir()),[])

    def test_cross_family_sidecar_hardlinks_cannot_modify_source_bytes(self):
        self.relay.write_bytes(b'synthetic source bytes')
        for suffix in SIDECARS:
            sidecar=Path(str(self.ledger)+suffix);os.link(self.relay,sidecar)
            self.rejected_before_sqlite()
            self.assertEqual(self.relay.read_bytes(),b'synthetic source bytes')
            self.assertFalse(self.ledger.exists());sidecar.unlink()

    def test_sidecar_to_sidecar_aliases_are_rejected_even_when_main_files_do_not_exist(self):
        source=Path(str(self.relay)+'-wal');source.write_bytes(b'synthetic wal bytes')
        for suffix in SIDECARS:
            alias=Path(str(self.ledger)+suffix);os.link(source,alias)
            self.rejected_before_sqlite();alias.unlink()
        self.assertFalse(self.ledger.exists());self.assertFalse(self.relay.exists())

    def test_same_family_aliases_are_invalid_too(self):
        self.ledger.write_bytes(b'synthetic existing ledger')
        alias=Path(str(self.ledger)+'-shm');os.link(self.ledger,alias)
        self.rejected_before_sqlite()
        self.assertEqual(self.ledger.read_bytes(),b'synthetic existing ledger')

    def test_symlink_sidecar_cannot_point_at_another_database(self):
        self.relay.write_bytes(b'synthetic source bytes')
        for suffix in SIDECARS:
            sidecar=Path(str(self.ledger)+suffix);sidecar.symlink_to(self.relay)
            self.rejected_before_sqlite();sidecar.unlink()
        self.assertEqual(self.relay.read_bytes(),b'synthetic source bytes')

    def test_regular_independent_files_and_sidecars_are_metadata_only(self):
        paths=[self.ledger,self.relay,self.native]
        for path in paths:
            path.write_bytes(b'synthetic bytes, not a database')
            Path(str(path)+'-wal').write_bytes(b'synthetic sidecar')
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.check(),tuple(paths))
        self.assertEqual({p.name:p.read_bytes() for p in self.root.iterdir()},before)

    def test_directories_in_reserved_file_slots_are_rejected(self):
        for name in ('ledger.sqlite','ledger.sqlite-wal','relay.sqlite-shm'):
            target=self.root/name;target.mkdir()
            self.rejected_before_sqlite();target.rmdir()

    def test_metadata_failure_does_not_expose_the_private_path_or_open_sqlite(self):
        with patch.object(Path,'stat',side_effect=PermissionError('synthetic private path')):
            self.rejected_before_sqlite()

