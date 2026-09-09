import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_swap import atomic_json, main, private_dir
import test_codex_swap
import test_live
from xswap_switch import read_private_json, switch_running


class ManualBridgeTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = test_live.BridgeTests.asyncTearDown
    async def asyncSetUp(self):
        await test_live.BridgeTests.asyncSetUp(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bridge.status_path = self.directory / 'status.json'

    def request(self, name='second', request_id='one', instance=None):
        atomic_json(self.directory / 'switch.json', {
            'account': name, 'id': request_id,
            'instance': instance or self.bridge.instance})

    async def test_idle_switch_keeps_thread_and_publishes_quota(self):
        self.bridge.starts['existing'] = {'threadId': 'existing', 'input': []}
        self.request()
        self.assertTrue(await self.bridge.apply_manual_switch())
        self.assertEqual(self.bridge.current, 'second')
        self.assertEqual(self.bridge.manual_state, 'applied')
        self.assertIn('existing', self.bridge.starts)
        self.assertEqual([c[0] for c in self.calls], ['account/login/start'])
        self.assertEqual(self.emitted[-1]['method'], 'account/rateLimits/updated')
        self.assertFalse(await self.bridge.apply_manual_switch())
        self.assertEqual(len(self.calls), 1)

    async def test_busy_switch_waits_for_all_turns(self):
        self.bridge.active.update(('a', 'b'))
        self.request()
        await self.bridge.apply_manual_switch()
        self.assertEqual(self.bridge.manual_state, 'pending')
        self.assertFalse(self.calls)
        self.bridge.active.remove('a')
        await self.bridge.apply_manual_switch()
        self.assertFalse(self.calls)
        self.bridge.active.remove('b')
        await self.bridge.apply_manual_switch()
        self.assertEqual(self.bridge.current, 'second')

    async def test_next_turn_applies_request_before_forwarding(self):
        self.request()
        await self.bridge.on_client({'id': 42, 'method': 'turn/start',
                                     'params': {'threadId': 'same', 'input': []}})
        self.assertEqual(self.bridge.current, 'second')
        self.assertEqual(self.sent[-1]['params']['threadId'], 'same')

    async def test_rejected_login_retains_account_and_does_not_retry_forever(self):
        async def reject(*args, **kwargs):
            raise RuntimeError('secret must not leak')
        self.bridge.rpc = reject
        self.request()
        await self.bridge.apply_manual_switch()
        self.assertEqual(self.bridge.current, 'first')
        self.assertEqual(self.bridge.manual_state, 'failed')
        self.assertFalse(await self.bridge.apply_manual_switch())

    async def test_stale_instance_and_symlink_are_ignored(self):
        self.request(instance='previous-process')
        await self.bridge.apply_manual_switch()
        self.assertFalse(self.calls)
        (self.directory / 'switch.json').rename(self.directory / 'other.json')
        (self.directory / 'switch.json').symlink_to(self.directory / 'other.json')
        await self.bridge.apply_manual_switch()
        self.assertFalse(self.calls)

    async def test_pending_request_can_be_replaced(self):
        self.bridge.active.add('a')
        self.request()
        await self.bridge.apply_manual_switch()
        self.request(name='first', request_id='two')
        self.bridge.active.clear()
        await self.bridge.apply_manual_switch()
        self.assertEqual(self.bridge.manual_request, 'two')
        self.assertEqual(self.bridge.current, 'first')


class SwitchCommandTests(unittest.TestCase):
    setUp = test_codex_swap.AccountTests.setUp

    def session(self, name, capable=True, running=True):
        directory = self.manager.root / 'auto' / 'cli-runs' / name
        for path in (directory.parent.parent, directory.parent, directory):
            private_dir(path)
        fd = os.open(directory / '.bridge.lock', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        if running:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {'account': 'main'}
        if capable:
            state.update(manualSwitchVersion=1, bridgeInstance=name)
        atomic_json(directory / 'status.json', state)
        return directory

    def test_only_live_capable_instances_receive_requests(self):
        live = self.session('live')
        old = self.session('old', capable=False)
        stopped = self.session('stopped', running=False)
        result = switch_running(self.manager, 'second', timeout=0)
        self.assertEqual(result['unconfirmed'], 1)
        self.assertEqual(result['unsupported'], 1)
        self.assertFalse((old / 'switch.json').exists())
        self.assertFalse((stopped / 'switch.json').exists())
        request = read_private_json(live / 'switch.json')
        self.assertEqual(request['instance'], 'live')
        self.assertEqual(request['account'], 'second')
        self.assertEqual((live / 'switch.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('token', json.dumps(request))

    def test_unsafe_auto_directory_is_reported_not_silently_skipped(self):
        live = self.session('live')
        auto = self.manager.root / 'auto'
        auto.chmod(0o755)
        result = switch_running(self.manager, 'second', timeout=0)
        self.assertEqual(result['unsafe'], str(auto))
        self.assertEqual(result['unconfirmed'], 0)
        self.assertFalse((live / 'switch.json').exists())
        auto.chmod(0o700)
        self.assertNotIn('unsafe', switch_running(self.manager, 'second', timeout=0))

    def test_acknowledgement_is_required_for_success(self):
        live = self.session('live')
        def ack(_):
            request = read_private_json(live / 'switch.json')
            atomic_json(live / 'status.json', dict(bridgeInstance='live',
                manualRequest=request['id'], manualState='applied'))
        with patch('xswap_switch.time.sleep', side_effect=ack):
            report = switch_running(self.manager, 'second')
        self.assertEqual(report['applied'], 1)
        self.assertEqual(report['unconfirmed'], 0)

    def test_default_only_does_not_send_requests(self):
        self.manager.register('main')
        with patch('codex_swap.Manager', return_value=self.manager), patch('xswap_switch.switch_running') as send:
            self.assertEqual(main(['switch', 'main', '--default-only']), 0)
            send.assert_not_called()

    def test_failed_delivery_is_nonzero(self):
        self.manager.register('main')
        with patch('codex_swap.Manager', return_value=self.manager), patch('xswap_switch.switch_running',
                return_value=dict(applied=0, pending=0, unsupported=0, failed=1, unconfirmed=0)):
            self.assertEqual(main(['switch', 'main']), 1)
