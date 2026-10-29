"""Private synthetic key files only; never access a deployment or credential store."""
import os
from pathlib import Path
import tempfile
import unittest

from backend.usage_ledger import LedgerError
from backend.usage_ledger_entrypoint import load_loop, secret, CONTINUATION_KEY
from backend.usage_ledger_secret_files import read_key_file


SUPPORTED = (all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'geteuid'))
             and os.open in os.supports_dir_fd)


class PrivateKeyFiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.private = self.root/'private'
        self.private.mkdir(mode=0o700)
        self.key = self.private/'continuation.key'
        self.key.write_text('synthetic-'+'k'*55)
        self.key.chmod(0o600)

    def test_private_file_and_existing_environment_return_same_bytes(self):
        value = self.key.read_text()
        self.assertEqual(secret({CONTINUATION_KEY:value}, CONTINUATION_KEY,
                                persistent_root=self.root), value.encode())
        if not SUPPORTED:
            with self.assertRaises(ValueError): read_key_file(str(self.key), self.root)
            return
        self.assertEqual(secret({CONTINUATION_KEY+'_FILE':str(self.key)}, CONTINUATION_KEY,
                                persistent_root=self.root), value.encode())

    def test_conflicting_sources_never_choose_or_read_a_file(self):
        from unittest.mock import patch
        with patch('backend.usage_ledger_secret_files.read_key_file', side_effect=AssertionError('no read')):
            with self.assertRaises(ValueError):
                secret({CONTINUATION_KEY:'k'*64,CONTINUATION_KEY+'_FILE':str(self.key)},
                       CONTINUATION_KEY,persistent_root=self.root)

    def test_disabled_does_not_inspect_file_configuration(self):
        class ModeOnly(dict):
            def get(self, key, default=None):
                if key != 'GUITING_LEDGER_MODE': raise AssertionError(key)
                return default
        self.assertIsNone(load_loop(ModeOnly(), persistent_root=None, relay_db=None, native_db=None))

    def test_outside_root_relative_missing_and_parent_escape_fail(self):
        for value in ('relative.key',str(self.root.parent/'outside.key'),
                      str(self.private/'..'/'other.key'),str(self.private/'missing.key')):
            with self.subTest(value=value):
                with self.assertRaises((ValueError,OSError)): read_key_file(value,self.root)

    def test_exposed_file_or_directory_permissions_fail(self):
        if not SUPPORTED:
            with self.assertRaises(ValueError): read_key_file(str(self.key),self.root)
            return
        for target in (self.key,self.private):
            original = target.stat().st_mode & 0o777
            target.chmod(0o644 if target==self.key else 0o755)
            try:
                with self.assertRaises(ValueError): read_key_file(str(self.key),self.root)
            finally: target.chmod(original)

    def test_symlink_file_directory_and_hardlink_are_rejected(self):
        if not SUPPORTED:
            with self.assertRaises(ValueError): read_key_file(str(self.key),self.root)
            return
        file_link=self.private/'linked.key';file_link.symlink_to(self.key)
        directory_link=self.root/'linked';directory_link.symlink_to(self.private,target_is_directory=True)
        for path in (file_link,directory_link/'continuation.key'):
            with self.assertRaises((ValueError,OSError)): read_key_file(str(path),self.root)
        os.link(self.key,self.private/'alias.key')
        with self.assertRaises(ValueError): read_key_file(str(self.key),self.root)

    def test_fifo_does_not_block_and_directory_is_not_a_key(self):
        if not SUPPORTED:
            with self.assertRaises(ValueError): read_key_file(str(self.key),self.root)
            return
        fifo=self.private/'pipe';os.mkfifo(fifo,0o600)
        for path in (fifo,self.private):
            with self.assertRaises((ValueError,OSError)): read_key_file(str(path),self.root)

    def test_bad_size_encoding_or_newline_is_rejected(self):
        for value in (b'x'*31,b'x'*513,b'\xff'*64,b'x'*63+b'\n'):
            self.key.write_bytes(value)
            with self.assertRaises((ValueError,OSError)):
                secret({CONTINUATION_KEY+'_FILE':str(self.key)},CONTINUATION_KEY,
                       persistent_root=self.root)
