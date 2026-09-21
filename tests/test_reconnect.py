import shlex
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_live import Pool

from xswap.codex_cli import WebSocketBridge, exit_notice, reconnect_command


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge = WebSocketBridge(Pool(), [], {}, socket=None)
        self.thread = '00000000-0000-4000-8000-000000000001'

    async def test_tracks_client_thread_not_background_thread(self):
        with patch('xswap.live.Bridge.on_client', return_value=None), patch('xswap.live.Bridge.on_server', return_value=None):
            await self.bridge.on_client({'id': 1, 'method': 'thread/resume'})
            await self.bridge.on_server({'id': 1, 'result': {'thread': {'id': self.thread}}})
            await self.bridge.on_server({'method': 'thread/started', 'params': {'thread': {'id': 'background'}}})
        self.assertEqual(self.bridge.resume_thread, self.thread)

    async def test_failed_resume_does_not_advertise_missing_thread(self):
        with patch('xswap.live.Bridge.on_client', return_value=None), patch('xswap.live.Bridge.on_server', return_value=None):
            await self.bridge.on_client({'id': 1, 'method': 'thread/resume'})
            await self.bridge.on_server({'id': 1, 'error': {'code': -1}})
        self.assertIsNone(self.bridge.resume_thread)

    async def test_current_user_turn_updates_resume_target(self):
        with patch('xswap.live.Bridge.on_client', return_value=None):
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
        with patch('xswap.codex_cli.saved_thread', return_value=True):
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
        with patch('xswap.codex_cli.saved_thread', side_effect=lambda home, tid: tid == self.thread):
            command = reconnect_command(pool, {'CODEX_HOME': '/tmp/home'}, self.bridge)
        self.assertEqual(shlex.split(command)[-1], self.thread)

    def test_no_hint_for_unsaved_thread(self):
        pool = Pool()
        pool.manager = SimpleNamespace(root=Path('/tmp/store'))
        self.bridge.remember_thread(self.thread)
        with patch('xswap.codex_cli.saved_thread', return_value=False):
            self.assertIsNone(reconnect_command(pool, {'CODEX_HOME': '/tmp/home'}, self.bridge))

    def test_conversation_change_is_published_to_status_once(self):
        # list/doctor/upgrade read conversationId from status.json; a session that moves
        # to another thread through the picker must not keep advertising the old one,
        # and an unchanged thread must not rewrite the record on every turn.
        import json
        import tempfile
        from pathlib import Path
        other = '00000000-0000-4000-8000-000000000002'
        with tempfile.TemporaryDirectory() as tmp:
            self.bridge.status_path = Path(tmp) / 'status.json'
            self.bridge.initialized = True
            self.bridge.remember_thread(self.thread)
            first = json.loads(self.bridge.status_path.read_text())
            self.bridge.remember_thread(self.thread)
            again = json.loads(self.bridge.status_path.read_text())
            self.bridge.remember_thread(other)
            second = json.loads(self.bridge.status_path.read_text())
        self.assertEqual(first['conversationId'], self.thread)
        self.assertEqual(first['event'], 'conversation-known')
        self.assertEqual(again['updatedAt'], first['updatedAt'])
        self.assertEqual(second['conversationId'], other)
        self.assertEqual(self.bridge.resume_candidates, [other, self.thread])

    def test_conversation_is_not_published_before_initialize_or_while_stopping(self):
        import tempfile
        from pathlib import Path
        other = '00000000-0000-4000-8000-000000000002'
        with tempfile.TemporaryDirectory() as tmp:
            self.bridge.status_path = Path(tmp) / 'status.json'
            self.bridge.remember_thread(self.thread)
            self.assertFalse(self.bridge.status_path.exists())
            self.bridge.initialized = True
            self.bridge.stopping = True
            self.bridge.remember_thread(other)
            self.assertFalse(self.bridge.status_path.exists())
        self.assertEqual(self.bridge.resume_thread, other)


class ExitNoticeTests(unittest.TestCase):
    command = 'env CODEX_SWAP_HOME=/s CODEX_HOME=/h xswap run --auto --accounts a,b -- resume t'

    def test_no_conversation_means_no_lines(self):
        self.assertEqual(exit_notice(0, None, None), [])
        self.assertEqual(exit_notice(0, None, self.command), [])

    def test_clean_exit_is_one_short_line_and_one_command(self):
        lines = exit_notice(0, 't', self.command)
        self.assertEqual(lines, ['xswap: Session ended. Resume this conversation:', self.command])
        self.assertNotIn('--remote', '\n'.join(lines))
        self.assertLess(len(lines[0]), 60)

    def test_unsaved_conversation_claims_no_success_and_gives_no_command(self):
        self.assertEqual(exit_notice(0, 't', None),
                         ['xswap: Session ended; this conversation was not saved, so there is nothing to resume.'])
        self.assertEqual(exit_notice(3, 't', None),
                         ['xswap: Codex exited with status 3; this conversation was not saved, so there is nothing to resume.'])

    def test_nonzero_status_is_named_not_hidden(self):
        self.assertEqual(exit_notice(2, 't', self.command),
                         ['xswap: Codex exited with status 2. Resume this conversation:', self.command])
        self.assertTrue(exit_notice(None, 't', None)[0].startswith('xswap: Codex exited with status None;'))
