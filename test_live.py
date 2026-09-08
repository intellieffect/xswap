import asyncio
import copy
import time
import unittest

import contextlib
import sys
from unittest.mock import patch

from xswap_live import AccountPool, Bridge, LiveError, failure_reason, quota_available, usage_failure
from xswap_usage import UsageError


def limits(left=100, reached=None):
    return {'rateLimits': {'limitId': 'codex', 'primary': {
        'usedPercent': 100 - left, 'windowDurationMins': 10080,
        'resetsAt': time.time() + 604800}, 'rateLimitReachedType': reached}}


class QuotaTests(unittest.TestCase):
    def test_unknown_is_not_eligible(self):
        self.assertIsNone(quota_available({}))
        self.assertIsNone(quota_available({'rateLimits': {'limitId': 'codex'}}))

    def test_any_exhausted_window_blocks_candidate(self):
        data = limits()
        data['rateLimits']['secondary'] = {'usedPercent': 100, 'windowDurationMins': 300}
        self.assertFalse(quota_available(data))
        self.assertTrue(quota_available(limits(1)))
        self.assertFalse(quota_available(limits(100, 'weekly')))

    def test_spark_requires_its_own_budget(self):
        self.assertIsNone(quota_available(limits(), 'gpt-5.3-codex-spark'))
        data = {'rateLimitsByLimitId': {'codex': limits()['rateLimits'],
                'spark': {**limits(0)['rateLimits'], 'limitId': 'spark'}}}
        self.assertFalse(quota_available(data, 'gpt-5.3-codex-spark'))
        self.assertTrue(quota_available(data, 'other-model'))

    def test_only_structured_usage_failure_retries(self):
        self.assertTrue(usage_failure({'status': 'failed', 'error': {'codexErrorInfo': 'usageLimitExceeded'}}))
        for info in ('unauthorized', 'httpConnectionFailed', 'contextWindowExceeded', 'rateLimitExceeded'):
            self.assertFalse(usage_failure({'status': 'failed', 'error': {'codexErrorInfo': info}}))
        self.assertFalse(usage_failure({'status': 'failed', 'error': {'message': 'usageLimitExceeded'}}))


class Pool:
    names = ['first', 'second']

    def __init__(self):
        self.quota = {'first': limits(), 'second': limits()}

    def prepare(self, name, require_quota=True):
        return {'accessToken': 'fake-' + name, 'chatgptAccountId': name,
                'chatgptPlanType': 'pro'}, self.quota[name]

    def refresh(self, name):
        return self.prepare(name)[0]


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.emitted, self.sent, self.calls = [], [], []
        self.pool = Pool()
        self.bridge = Bridge(self.pool, [], {}, self.emitted.append)
        self.bridge.booting = False
        self.bridge.initialized = True
        self.bridge.current_id = 'first'
        self.bridge.last_quota = limits()
        self.bridge.checked_at = time.monotonic()
        self.bridge.status = lambda *args, **kwargs: None

        async def rpc(method, params, timeout=20):
            self.calls.append((method, copy.deepcopy(params)))
            return {}

        async def send(message):
            self.sent.append(copy.deepcopy(message))

        self.bridge.rpc, self.bridge.send = rpc, send

    async def asyncTearDown(self):
        for task in list(self.bridge.tasks):
            task.cancel()
        await asyncio.gather(*self.bridge.tasks, return_exceptions=True)

    async def failed(self, thread='thread-1'):
        await self.bridge.on_server({'method': 'turn/completed', 'params': {
            'threadId': thread, 'turn': {'id': 'turn-1', 'status': 'failed',
            'error': {'codexErrorInfo': 'usageLimitExceeded'}}}})
        await asyncio.gather(*list(self.bridge.tasks))

    async def test_switches_same_thread_and_keeps_original_failure_visible(self):
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {
            'threadId': 'thread-1', 'input': [{'type': 'text', 'text': 'do work'}],
            'approvalPolicy': 'on-request', 'model': 'example'}})
        await self.failed()
        self.assertEqual(self.bridge.current, 'second')
        self.assertEqual([x[0] for x in self.calls], ['account/login/start', 'turn/start'])
        continuation = self.calls[-1][1]
        self.assertEqual(continuation['threadId'], 'thread-1')
        self.assertEqual(continuation['approvalPolicy'], 'on-request')
        self.assertNotEqual(continuation['input'], [{'type': 'text', 'text': 'do work'}])
        self.assertEqual(self.emitted[0]['params']['turn']['status'], 'failed')

    async def test_all_accounts_exhausted_stops_without_loop(self):
        self.bridge.starts['thread-1'] = {'threadId': 'thread-1', 'input': []}
        self.pool.quota['second'] = limits(0)
        await self.failed()
        self.assertEqual(self.bridge.current, 'first')
        self.assertFalse(self.calls)
        self.assertFalse(self.bridge.failed)

    async def test_one_attempt_per_account_per_chain(self):
        self.bridge.starts['thread-1'] = {'threadId': 'thread-1', 'input': []}
        await self.failed()
        await self.failed()
        self.assertEqual([c[0] for c in self.calls].count('turn/start'), 1)

    async def test_does_not_switch_while_another_turn_is_active(self):
        self.bridge.starts['thread-1'] = {'threadId': 'thread-1', 'input': []}
        self.bridge.active.add('other')
        await self.failed()
        self.assertFalse(self.calls)
        await self.bridge.on_server({'method': 'turn/completed', 'params': {
            'threadId': 'other', 'turn': {'id': 'other-turn', 'status': 'completed'}}})
        await asyncio.gather(*list(self.bridge.tasks))
        self.assertEqual(self.bridge.current, 'second')

    async def test_interrupt_cancels_pending_continuation(self):
        self.bridge.starts['thread-1'] = {'threadId': 'thread-1', 'input': []}
        self.bridge.active.add('other')
        await self.failed()
        await self.bridge.on_client({'id': 5, 'method': 'turn/interrupt', 'params': {'threadId': 'thread-1', 'turnId': 'turn-1'}})
        self.bridge.active.clear()
        await self.bridge.continue_failed()
        self.assertFalse(self.calls)

    async def test_preflight_switch_before_exhausted_account_gets_work(self):
        self.bridge.last_quota = limits(0)
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': 't', 'input': []}})
        self.assertEqual(self.bridge.current, 'second')
        self.assertEqual(self.sent[-1]['method'], 'turn/start')

    async def test_unknown_current_quota_does_not_block_work(self):
        self.bridge.last_quota = {}
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': 't', 'input': []}})
        self.assertEqual(self.bridge.current, 'first')
        self.assertEqual(len(self.sent), 1)

    async def test_login_logout_cannot_overwrite_runtime_credentials(self):
        for method in ('account/login/start', 'account/logout'):
            await self.bridge.on_client({'id': 1, 'method': method, 'params': {}})
        self.assertFalse(self.sent)
        self.assertTrue(all('error' in x for x in self.emitted))

    async def test_approvals_and_tool_requests_pass_through(self):
        approval = {'id': 100, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 't'}}
        await self.bridge.on_server(approval)
        await self.bridge.on_client({'id': 100, 'result': {'decision': 'decline'}})
        self.assertEqual(self.emitted[-1], approval)
        self.assertEqual(self.sent[-1]['result']['decision'], 'decline')

    async def test_refresh_rejects_account_mismatch(self):
        await self.bridge.refresh({'id': 6, 'params': {'previousAccountId': 'different'}})
        self.assertIn('error', self.sent[-1])
        self.assertNotIn('fake-', str(self.sent[-1]))

    async def test_start_error_releases_active_turn(self):
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': 't', 'input': []}})
        await self.bridge.on_server({'id': 1, 'error': {'code': -1, 'message': 'bad'}})
        self.assertFalse(self.bridge.active)

    async def test_alias_of_exhausted_identity_is_not_reused(self):
        self.pool.names = ['first', 'second', 'alias']
        prepare = self.pool.prepare
        self.pool.prepare = lambda name: prepare('first' if name == 'alias' else name)
        self.bridge.starts['thread-1'] = {'threadId': 'thread-1', 'input': []}
        await self.failed()
        await self.failed()
        self.assertEqual([c[0] for c in self.calls].count('turn/start'), 1)

    async def test_interrupt_during_candidate_lookup_does_not_continue(self):
        self.bridge.starts['t'] = {'threadId': 't', 'input': []}
        self.bridge.failed['t'] = self.bridge.starts['t']
        async def choose(*args):
            self.bridge.failed.pop('t', None)
            return True
        self.bridge.choose = choose
        await self.bridge.continue_failed()
        self.assertFalse(self.calls)


class InitializeTests(unittest.IsolatedAsyncioTestCase):
    """A usage-service outage at startup must not kill the session (2026-09-08 incident)."""

    async def asyncSetUp(self):
        await test_setup(self)
        self.bridge.booting, self.bridge.initialized = True, False
        self.bridge.current_id, self.bridge.last_quota = None, None
        self.events = []
        self.bridge.status = lambda event, **extra: self.events.append((event, extra))

    asyncTearDown = BridgeTests.asyncTearDown

    async def initialize(self):
        await self.bridge.on_client({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'x'}}})

    async def test_usage_outage_at_startup_keeps_session_and_rechecks_before_first_turn(self):
        def prepare(name, require_quota=True):
            if require_quota:
                raise UsageError('usage request timed out')
            return {'accessToken': 'fake-' + name, 'chatgptAccountId': name, 'chatgptPlanType': 'pro'}, None
        self.pool.prepare = prepare
        await self.initialize()
        self.assertTrue(self.bridge.initialized)
        self.assertEqual([c[0] for c in self.calls], ['initialize', 'account/login/start'])
        self.assertEqual(self.emitted[-1]['id'], 1)
        self.assertIsNone(self.bridge.last_quota)
        self.assertEqual(self.bridge.checked_at, 0)
        self.assertEqual(self.events[-1][0], 'ready')
        self.assertFalse(self.events[-1][1].get('quotaKnown', True))

    async def test_credential_problems_at_startup_still_stop_the_bridge(self):
        def prepare(name, require_quota=True):
            raise LiveError('ChatGPT access token needs refresh or sign-in')
        self.pool.prepare = prepare
        with self.assertRaises(LiveError):
            await self.initialize()
        self.assertFalse(self.bridge.initialized)

    async def test_startup_with_quota_marks_it_known(self):
        await self.initialize()
        self.assertTrue(self.events[-1][1].get('quotaKnown'))
        self.assertGreater(self.bridge.checked_at, 0)


class PoolPrepareTests(unittest.TestCase):
    def make_pool(self):
        pool = object.__new__(AccountPool)
        pool.codex = 'fixture-codex'

        class Manager:
            def enabled_accounts(self):
                return [('main', None)]

            def account(self, name):
                return name, '/fixture/home'

            def env(self, home):
                return {}
        pool.manager = Manager()
        return pool

    def test_quota_outage_is_optional_only_when_asked(self):
        pool = self.make_pool()
        with patch('xswap_live.check_file_store', create=True), \
                patch('codex_swap.check_file_store'), patch('xswap_live.read_auth'), \
                patch('xswap_live.load_credentials', return_value={'accessToken': 'x'}), \
                patch('xswap_live.read_limits', side_effect=UsageError('usage request timed out')):
            with self.assertRaises(UsageError):
                pool.prepare('main')
            credentials, raw = pool.prepare('main', require_quota=False)
        self.assertEqual(credentials, {'accessToken': 'x'})
        self.assertIsNone(raw)


class FailureReasonTests(unittest.TestCase):
    def test_only_curated_messages_are_exposed(self):
        self.assertEqual(failure_reason(LiveError('app-server exited')), 'app-server exited')
        self.assertEqual(failure_reason(UsageError('usage request timed out')), 'usage request timed out')
        self.assertEqual(failure_reason(asyncio.TimeoutError()), 'app-server request timed out')
        secret = RuntimeError('token=sk-secret payload')
        self.assertEqual(failure_reason(secret), 'RuntimeError')
        self.assertNotIn('secret', failure_reason(secret))


class RunFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopped_status_and_bridge_carry_the_reason(self):
        events = []
        bridge = Bridge(Pool(), [sys.executable, '-c', 'pass'], {})
        bridge.status = lambda event, **extra: events.append((event, extra))

        async def idle():
            await asyncio.Event().wait()
        bridge.client_reader = idle
        bridge.manual_switch_reader = idle
        with self.assertRaises(LiveError):
            await bridge.run()
        self.assertEqual(events[-1], ('stopped', {'reason': 'app-server exited'}))
        self.assertEqual(bridge.failure, 'app-server exited')


async def test_setup(case):
    await BridgeTests.asyncSetUp(case)
