import asyncio
import copy
import time
import unittest

from xswap_live import Bridge, quota_available, usage_failure


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

    def prepare(self, name):
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
