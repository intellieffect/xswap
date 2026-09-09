import shlex
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xswap_cli import WebSocketBridge, reconnect_command
from test_live import Pool


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = WebSocketBridge(Pool(), [], {}, socket=None)
        self.thread = '00000000-0000-4000-8000-000000000001'

    async def test_tracks_client_thread_not_background_thread(self):
        with patch('xswap_live.Bridge.on_client', return_value=None), patch('xswap_live.Bridge.on_server', return_value=None):
            await self.bridge.on_client({'id': 1, 'method': 'thread/resume'})
            await self.bridge.on_server({'id': 1, 'result': {'thread': {'id': self.thread}}})
            await self.bridge.on_server({'method': 'thread/started', 'params': {'thread': {'id': 'background'}}})
        self.assertEqual(self.bridge.resume_thread, self.thread)

    async def test_failed_resume_does_not_advertise_missing_thread(self):
        with patch('xswap_live.Bridge.on_client', return_value=None), patch('xswap_live.Bridge.on_server', return_value=None):
            await self.bridge.on_client({'id': 1, 'method': 'thread/resume'})
            await self.bridge.on_server({'id': 1, 'error': {'code': -1}})
        self.assertIsNone(self.bridge.resume_thread)

    async def test_current_user_turn_updates_resume_target(self):
        with patch('xswap_live.Bridge.on_client', return_value=None):
            await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': self.thread}})
        self.assertEqual(self.bridge.resume_thread, self.thread)
        self.bridge.remember_thread('bad; command')
        self.assertEqual(self.bridge.resume_thread, self.thread)

    def test_command_preserves_custom_homes_current_account_and_quotes_paths(self):
        pool = Pool()
        pool.manager = SimpleNamespace(root=Path('/tmp/store with spaces;$(touch nope)'))
        self.bridge.remember_thread(self.thread)
        self.bridge.current = 'outside'
        env = {'CODEX_HOME': '/tmp/original home', 'OPENAI_API_KEY': 'do-not-print'}
        with patch('xswap_cli.saved_thread', return_value=True):
            command = reconnect_command(pool, env, self.bridge)
        self.assertEqual(shlex.split(command), ['env',
            'CODEX_SWAP_HOME=/tmp/store with spaces;$(touch nope)',
            'CODEX_HOME=/tmp/original home', 'xswap', 'run', '--auto',
            '--accounts', 'outside,first,second', '--', 'resume', self.thread])
        self.assertNotIn('--remote', command)
        self.assertNotIn('do-not-print', command)

    def test_no_hint_before_a_conversation_is_known(self):
        self.assertIsNone(reconnect_command(Pool(), {}, self.bridge))

    def test_hint_ignores_unsaved_bootstrap_thread(self):
        pool = Pool()
        pool.manager = SimpleNamespace(root=Path('/tmp/store'))
        self.bridge.remember_thread(self.thread)
        self.bridge.remember_thread('00000000-0000-4000-8000-000000000002')
        with patch('xswap_cli.saved_thread', side_effect=lambda home, tid: tid == self.thread):
            command = reconnect_command(pool, {'CODEX_HOME': '/tmp/home'}, self.bridge)
        self.assertEqual(shlex.split(command)[-1], self.thread)

    def test_no_hint_for_unsaved_thread(self):
        pool = Pool()
        pool.manager = SimpleNamespace(root=Path('/tmp/store'))
        self.bridge.remember_thread(self.thread)
        with patch('xswap_cli.saved_thread', return_value=False):
            self.assertIsNone(reconnect_command(pool, {'CODEX_HOME': '/tmp/home'}, self.bridge))
