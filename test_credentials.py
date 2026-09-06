import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_swap import Manager, SwapError, atomic_json
from xswap_credentials import CredentialError, read_auth
from xswap_live import AccountPool, LiveError, load_credentials


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / 'auth.json'
        self.data = {'auth_mode': 'chatgpt', 'tokens': {'access_token': 'fixture-only'}}
        atomic_json(self.path, self.data)

    def test_private_file_and_readonly_file_are_accepted(self):
        for mode in (0o600, 0o400):
            self.path.chmod(mode)
            self.assertEqual(read_auth(self.home), self.data)

    def test_exposed_modes_rejected_without_mutation(self):
        before = self.path.read_bytes()
        for mode in (0o644, 0o640, 0o620, 0o604):
            self.path.chmod(mode)
            with self.assertRaises(CredentialError): read_auth(self.home)
            with self.assertRaises(LiveError): load_credentials(self.home)
            self.assertEqual(self.path.stat().st_mode & 0o777, mode)
            self.assertEqual(self.path.read_bytes(), before)

    def test_link_rejected(self):
        target = self.home / 'target'
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(CredentialError): read_auth(self.home)
        self.assertEqual(json.loads(target.read_text()), self.data)

    def test_fifo_and_directory_rejected_without_blocking(self):
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(CredentialError): read_auth(self.home)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(CredentialError): read_auth(self.home)

    def test_foreign_owner_rejected(self):
        with patch('xswap_credentials.os.getuid', return_value=os.getuid()+1):
            with self.assertRaises(CredentialError): read_auth(self.home)

    def test_registration_and_refresh_reject_before_mutation_or_spawn(self):
        manager = Manager(self.home/'store', self.home)
        manager.register('main')
        other = manager.prepare('other')
        atomic_json(other/'auth.json', self.data)
        pool = AccountPool(manager, ['main', 'other'], 'fixture-codex')
        before = manager.registry.read_bytes()
        self.path.chmod(0o644)
        with self.assertRaises(SwapError): manager.register('rejected')
        with patch('xswap_live.read_limits') as spawn:
            with self.assertRaises(LiveError): pool.prepare('main')
            spawn.assert_not_called()
        self.assertEqual(manager.registry.read_bytes(), before)

    def test_atomic_replacement_after_open_does_not_change_read_target(self):
        original = os.fstat
        def replace_after_check(fd):
            info = original(fd)
            atomic_json(self.path, {'replacement': True})
            return info
        with patch('xswap_credentials.os.fstat', side_effect=replace_after_check):
            self.assertEqual(read_auth(self.home), self.data)
