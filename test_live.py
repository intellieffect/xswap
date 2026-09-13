import asyncio
import base64
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest

import sys
from unittest.mock import patch

from codex_swap import Manager, atomic_json, identity
from xswap_live import (BRIDGE_LOG_NAME, AccountPool, Bridge, LiveError, SignInRequired, append_log_line,
                        failure_reason, quota_available, sign_in_required, usage_failure)
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

    def first_available(self):
        return self.names[0]


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

    async def test_turn_start_moves_away_from_a_current_account_the_service_rejected(self):
        self.bridge.checked_at = 0  # force a fresh check before the turn
        prepare = self.pool.prepare

        def rejecting(name, require_quota=True):
            if name == 'first':
                raise SignInRequired(sign_in_required('first'))
            return prepare(name, require_quota)
        self.pool.prepare = rejecting
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': 't', 'input': []}})
        self.assertEqual(self.bridge.current, 'second')
        self.assertEqual([c[0] for c in self.calls], ['account/login/start'])
        self.assertEqual(self.calls[0][1]['accessToken'], 'fake-second')
        self.assertEqual(self.sent[-1]['method'], 'turn/start')

    async def test_turn_start_keeps_the_current_account_through_a_usage_outage(self):
        self.bridge.checked_at = 0
        prepare = self.pool.prepare

        def outage(name, require_quota=True):
            if name == 'first':
                raise UsageError('usage request timed out')
            return prepare(name, require_quota)
        self.pool.prepare = outage
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': 't', 'input': []}})
        self.assertEqual(self.bridge.current, 'first')
        self.assertFalse(self.calls)
        self.assertEqual(self.sent[-1]['method'], 'turn/start')


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
        self.assertIs(self.bridge.quota_known, False)

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
        self.assertIs(self.bridge.quota_known, True)
        self.assertGreater(self.bridge.checked_at, 0)

    async def test_startup_skips_a_pool_member_recorded_as_rejected(self):
        self.pool.first_available = lambda: 'second'
        await self.initialize()
        self.assertTrue(self.bridge.initialized)
        self.assertEqual(self.bridge.current, 'second')
        self.assertEqual([c[0] for c in self.calls], ['initialize', 'account/login/start'])
        self.assertEqual(self.calls[1][1]['accessToken'], 'fake-second')
        self.assertEqual(self.events[-1][0], 'ready')

    async def test_startup_falls_back_when_the_first_member_is_rejected_live(self):
        def prepare(name, require_quota=True):
            if name == 'first':
                raise SignInRequired(sign_in_required('first'))
            return {'accessToken': 'fake-' + name, 'chatgptAccountId': name, 'chatgptPlanType': 'pro'}, limits()
        self.pool.prepare = prepare
        await self.initialize()
        self.assertTrue(self.bridge.initialized)
        self.assertEqual(self.bridge.current, 'second')
        self.assertEqual([c[0] for c in self.calls], ['initialize', 'account/login/start'])
        self.assertEqual(self.calls[1][1]['accessToken'], 'fake-second')
        self.assertEqual(self.emitted[-1]['id'], 1)
        self.assertEqual(self.events[-1][0], 'ready')

    async def test_startup_stops_with_the_reason_when_every_member_is_rejected(self):
        def prepare(name, require_quota=True):
            raise SignInRequired(sign_in_required(name))
        self.pool.prepare = prepare
        with self.assertRaises(SignInRequired) as caught:
            await self.initialize()
        self.assertFalse(self.bridge.initialized)
        self.assertEqual(failure_reason(caught.exception), 'sign-in required; run: xswap login first')
        self.assertEqual(self.events[-1][0], 'no-available-account')
        # item 4 hands the exception to status(), so the reason is classified there.
        self.assertIn(('candidate-unavailable', 'second', 'sign-in required; run: xswap login second'),
                      [(event, extra.get('candidate'), failure_reason(extra['exc']))
                       for event, extra in self.events if event == 'candidate-unavailable'])


class PoolPrepareTests(unittest.TestCase):
    def make_pool(self):
        pool = object.__new__(AccountPool)
        pool.codex = 'fixture-codex'

        class FakeManager:
            def __init__(self):
                self.recorded, self.cleared = [], []

            def enabled_accounts(self):
                return [('main', None)]

            def account(self, name):
                # A Path, not a str: prepare now reads identity(home) before probing.
                return name, Path('/fixture/home')

            def env(self, home):
                return {}

            def auth_failure(self, name, label):
                return None

            def remember_auth_failure(self, name, label):
                self.recorded.append((name, label))

            def clear_auth_failure(self, name):
                self.cleared.append(name)
        pool.manager = FakeManager()
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
        self.assertEqual(pool.manager.recorded, [])  # an outage is never a sign-in failure


def fake_jwt(claims):
    header = base64.urlsafe_b64encode(b'{}').rstrip(b'=')
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=')
    return (header + b'.' + payload + b'.fixture-sig').decode()


class PoolAuthStateTests(unittest.TestCase):
    """AccountPool.prepare against a real Manager store: the rejected-login record in
    auth-state.json is written by a live rejection, consulted before any probe, and
    only counts for the current login label."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'original'
        self.source.mkdir()
        self.source.joinpath('config.toml').write_text('model = "example"\n')
        token = fake_jwt({'exp': time.time() + 100000, 'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-main'}})
        self.auth = {'auth_mode': 'chatgpt', 'tokens': {'access_token': token, 'refresh_token': 'fixture-refresh'}}
        atomic_json(self.source / 'auth.json', self.auth)
        self.manager = Manager(self.base / 'store', self.source)
        self.manager.register('main')
        second = self.manager.prepare('second')
        atomic_json(second / 'auth.json', self.auth)
        self.pool = AccountPool(self.manager, ['main', 'second'], 'fixture-codex')
        self.label = identity(self.source)

    def test_fresh_rejection_is_recorded_and_raised_as_sign_in_required(self):
        with patch('xswap_live.read_limits', side_effect=UsageError('sign in again to read usage')) as fetch:
            with self.assertRaises(SignInRequired) as caught:
                self.pool.prepare('main')
        self.assertEqual(str(caught.exception), 'sign-in required; run: xswap login main')
        self.assertEqual(fetch.call_count, 1)
        path = self.manager.auth_state_path()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        raw = path.read_text()
        self.assertNotIn('fixture-refresh', raw)
        self.assertNotIn(self.auth['tokens']['access_token'], raw)
        state = json.loads(raw)
        self.assertEqual(state['main']['identity'], self.label)
        self.assertEqual(state['main']['reason'], 'sign-in required')
        self.assertNotIn('second', state)

    def test_recorded_rejection_skips_the_probe_even_when_quota_is_optional(self):
        self.manager.remember_auth_failure('main', self.label)
        with patch('xswap_live.read_limits') as fetch:
            for require_quota in (True, False):
                with self.subTest(require_quota=require_quota), self.assertRaises(SignInRequired):
                    self.pool.prepare('main', require_quota=require_quota)
        fetch.assert_not_called()

    def test_record_for_a_previous_login_label_is_ignored_and_swept_by_a_success(self):
        self.manager.remember_auth_failure('main', 'previous@example.test')
        with patch('xswap_live.read_limits', return_value=limits()) as fetch:
            credentials, raw = self.pool.prepare('main')
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(credentials['chatgptAccountId'], 'acct-main')
        self.assertEqual(raw['rateLimits']['limitId'], 'codex')  # a real response came back
        self.assertNotIn('main', json.loads(self.manager.auth_state_path().read_text()))

    def test_first_available_skips_recorded_members_and_falls_back_to_the_first(self):
        self.assertEqual(self.pool.first_available(), 'main')
        self.manager.remember_auth_failure('main', self.label)
        self.assertEqual(self.pool.first_available(), 'second')
        self.manager.remember_auth_failure('second', identity(self.manager.account('second')[1]))
        self.assertEqual(self.pool.first_available(), 'main')
        self.manager.remember_auth_failure('main', 'previous@example.test')  # stale label: main is available again
        self.assertEqual(self.pool.first_available(), 'main')

    def test_outage_keeps_the_record_untouched(self):
        with patch('xswap_live.read_limits', side_effect=UsageError('usage request timed out')):
            with self.assertRaises(UsageError):
                self.pool.prepare('main')
        self.assertFalse(self.manager.auth_state_path().exists())


class FailureReasonTests(unittest.TestCase):
    def test_only_curated_messages_are_exposed(self):
        self.assertEqual(failure_reason(LiveError('app-server exited')), 'app-server exited')
        self.assertEqual(failure_reason(UsageError('usage request timed out')), 'usage request timed out')
        self.assertEqual(failure_reason(TimeoutError()), 'app-server request timed out')
        secret = RuntimeError('token=sk-secret payload')
        self.assertEqual(failure_reason(secret), 'RuntimeError')
        self.assertNotIn('secret', failure_reason(secret))

    def test_store_and_os_errors_are_classified_without_paths(self):
        from codex_swap import SwapError
        missing = FileNotFoundError(2, 'No such file or directory', '/Users/x/private/codex')
        cases = [
            (SwapError('Cannot read valid config.toml at /Users/x/private'), 'account config.toml is unreadable'),
            (SwapError("/Users/x/private: credential storage is 'keyring'; this version supports file storage only. No credentials changed."),
             'account uses non-file credential storage'),
            (SwapError('No matching account. Run: xswap register main, or xswap add NAME'), 'account is not registered'),
            (SwapError('Unsafe storage directory: /Users/x/private'), 'account store is unusable (xswap doctor)'),
            (missing, 'local I/O error: No such file or directory'),
            (TimeoutError(), 'app-server request timed out'),  # an OSError subclass; must keep its own text
        ]
        for exc, expected in cases:
            self.assertEqual(failure_reason(exc), expected)
            self.assertNotIn('/Users', failure_reason(exc))


def recording_setup(case):
    """After test_setup: a real run dir, so status.json and bridge.log are written; stderr stays quiet."""
    case.temporary = tempfile.TemporaryDirectory()
    case.addCleanup(case.temporary.cleanup)
    case.run_dir = Path(case.temporary.name)
    case.bridge.status_path = case.run_dir / 'status.json'
    del case.bridge.status  # drop BridgeTests' no-op override: the real method writes the records
    case.bridge.status_log = lambda event: None


class BridgeLogTests(unittest.IsolatedAsyncioTestCase):
    """Every swallowed failure leaves a classified reason in status.json and a line in bridge.log.

    2026-09-10: three manual switches ended as manualState "failed" with reason null and no
    log; the cause could not be reconstructed.
    """
    asyncTearDown = BridgeTests.asyncTearDown

    async def asyncSetUp(self):
        await test_setup(self)
        recording_setup(self)

    def state(self):
        return json.loads((self.run_dir / 'status.json').read_text())

    def log_lines(self):
        return (self.run_dir / BRIDGE_LOG_NAME).read_text().splitlines()

    def request(self, name='second', request_id='req-1'):
        from codex_swap import atomic_json
        atomic_json(self.run_dir / 'switch.json', {'account': name, 'id': request_id, 'instance': self.bridge.instance})

    async def test_failed_manual_switch_records_reason_and_log_line(self):
        def prepare(name, require_quota=True):
            raise UsageError('usage service unavailable')
        self.pool.prepare = prepare
        self.request()
        self.assertFalse(await self.bridge.apply_manual_switch())
        state = self.state()
        self.assertEqual((state['event'], state['manualState'], state['account']), ('manual-switch-failed', 'failed', 'first'))
        self.assertEqual(state['manualReason'], 'usage service unavailable')
        self.assertEqual(state['reason'], 'usage service unavailable')
        self.assertEqual(state['candidate'], 'second')
        self.assertEqual({k: state['lastFailure'][k] for k in ('event', 'account', 'candidate', 'reason')},
                         {'event': 'manual-switch-failed', 'account': 'first', 'candidate': 'second', 'reason': 'usage service unavailable'})
        log = self.run_dir / BRIDGE_LOG_NAME
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        lines = self.log_lines()
        self.assertEqual([line.split(' ')[1] for line in lines], ['manual-switch-applying', 'manual-switch-failed'])
        self.assertRegex(lines[-1], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} manual-switch-failed ')
        self.assertTrue(lines[-1].endswith(' account="first" candidate="second" reason="usage service unavailable" exc="UsageError"'), lines[-1])

    async def test_manual_reason_persists_after_later_events_and_clears_on_success(self):
        # 2026-09-10: `stopped` overwrote `manual-switch-failed`, and the record ended with reason: null.
        outcomes = iter([RuntimeError('token=sk-secret payload'), None])
        prepare = self.pool.prepare

        def flaky(name, require_quota=True):
            error = next(outcomes)
            if error:
                raise error
            return prepare(name, require_quota)
        self.pool.prepare = flaky
        self.request()
        self.assertFalse(await self.bridge.apply_manual_switch())
        self.bridge.status('policy-applied')
        state = self.state()
        self.assertEqual((state['event'], state['manualState'], state['manualReason']), ('policy-applied', 'failed', 'RuntimeError'))
        self.assertNotIn('reason', state)  # `reason` belongs to the current event; manualReason persists
        self.assertEqual(state['lastFailure']['reason'], 'RuntimeError')
        self.request(request_id='req-2')
        self.assertTrue(await self.bridge.apply_manual_switch())
        state = self.state()
        self.assertEqual((state['manualState'], state['manualReason'], state['account']), ('applied', None, 'second'))
        self.assertEqual(state['lastFailure']['event'], 'manual-switch-failed')  # history stays until the bridge exits
        text = (self.run_dir / BRIDGE_LOG_NAME).read_text() + (self.run_dir / 'status.json').read_text()
        self.assertNotIn('sk-secret', text)
        self.assertNotIn('fake-', text)

    async def test_pending_manual_switch_says_what_it_waits_for(self):
        self.bridge.active.update(('t1', 't2'))
        self.request()
        self.assertFalse(await self.bridge.apply_manual_switch())
        state = self.state()
        self.assertEqual((state['manualState'], state['manualReason']), ('pending', 'waiting for 2 active turn(s) to finish'))
        self.assertIsNone(state['lastFailure'])
        self.assertTrue(self.log_lines()[-1].endswith(
            ' manual-switch-pending account="first" candidate="second" reason="waiting for 2 active turn(s) to finish"'))
        self.bridge.active.clear()
        self.assertTrue(await self.bridge.apply_manual_switch())
        self.assertEqual((self.state()['manualState'], self.state()['manualReason']), ('applied', None))

    async def test_no_available_account_summarizes_every_candidate(self):
        self.pool.names = ['first', 'second', 'third', 'fourth', 'fifth']
        self.pool.quota.update(third=limits(0), fifth={})
        prepare = self.pool.prepare

        def flaky(name, require_quota=True):
            if name == 'second':
                raise LiveError('ChatGPT access token needs refresh or sign-in')
            if name == 'fourth':
                return prepare('first')[0], limits()  # an alias of the exhausted current identity
            return prepare(name, require_quota)
        self.pool.prepare = flaky
        self.bridge.last_quota = limits(0)
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': 't', 'input': []}})
        self.assertEqual(self.bridge.current, 'first')
        self.assertEqual(self.sent[-1]['method'], 'turn/start')  # the turn still goes out on the current account
        state = self.state()
        self.assertEqual(state['event'], 'no-available-account')
        self.assertEqual(state['reason'], 'second: ChatGPT access token needs refresh or sign-in; '
                         'third: no quota above the weekly reserve; fourth: same identity as an excluded account; '
                         'fifth: quota unknown')
        self.assertEqual(state['lastFailure']['event'], 'no-available-account')
        lines = self.log_lines()
        self.assertEqual([line.split(' ')[1] for line in lines],
                         ['policy-applied', 'candidate-unavailable', 'candidate-skipped', 'candidate-skipped',
                          'candidate-skipped', 'no-available-account'])
        self.assertTrue(lines[1].endswith(
            ' account="first" candidate="second" reason="ChatGPT access token needs refresh or sign-in" exc="LiveError"'), lines[1])

    async def test_failed_continuation_records_reason(self):
        async def rpc(method, params, timeout=20):
            self.calls.append((method, copy.deepcopy(params)))
            if method == 'turn/start':
                raise LiveError('Codex rejected turn/start')
            return {}
        self.bridge.rpc = rpc
        self.bridge.starts['thread-1'] = {'threadId': 'thread-1', 'input': []}
        await BridgeTests.failed(self, 'thread-1')
        self.assertEqual(self.bridge.current, 'second')
        self.assertFalse(self.bridge.active)
        state = self.state()
        self.assertEqual((state['event'], state['reason']), ('continuation-failed', 'Codex rejected turn/start'))
        self.assertEqual({k: state['lastFailure'][k] for k in ('event', 'account', 'reason')},
                         {'event': 'continuation-failed', 'account': 'second', 'reason': 'Codex rejected turn/start'})
        self.assertTrue(self.log_lines()[-1].endswith(
            ' continuation-failed account="second" reason="Codex rejected turn/start" exc="LiveError"'))

    async def test_failed_quota_check_is_logged_and_does_not_block_the_turn(self):
        self.bridge.checked_at = 0

        def prepare(name, require_quota=True):
            raise UsageError('usage request timed out')
        self.pool.prepare = prepare
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': 't', 'input': []}})
        self.assertEqual(self.sent[-1]['method'], 'turn/start')
        self.assertEqual((self.state()['event'], self.state()['reason']), ('quota-check-failed', 'usage request timed out'))
        self.assertTrue(self.log_lines()[-1].endswith(
            ' quota-check-failed account="first" reason="usage request timed out" exc="UsageError"'))

    async def test_rejected_candidate_and_its_login_command_reach_the_record_and_the_log(self):
        prepare = self.pool.prepare

        def rejecting(name, require_quota=True):
            if name == 'second':
                raise SignInRequired(sign_in_required('second'))
            return prepare(name, require_quota)
        self.pool.prepare = rejecting
        self.assertFalse(await self.bridge.choose({'first'}))
        self.assertEqual(self.bridge.current, 'first')
        state = self.state()
        self.assertEqual((state['event'], state['reason']),
                         ('no-available-account', 'second: sign-in required; run: xswap login second'))
        self.assertEqual({k: state['lastFailure'][k] for k in ('event', 'account', 'reason')},
                         {'event': 'no-available-account', 'account': 'first',
                          'reason': 'second: sign-in required; run: xswap login second'})
        self.assertEqual([line.split(' ')[1] for line in self.log_lines()],
                         ['candidate-unavailable', 'no-available-account'])
        self.assertTrue(self.log_lines()[0].endswith(
            ' candidate-unavailable account="first" candidate="second"'
            ' reason="sign-in required; run: xswap login second" exc="SignInRequired"'), self.log_lines()[0])

    async def test_rejected_current_account_switches_without_a_quota_check_failure(self):
        self.bridge.checked_at = 0
        prepare = self.pool.prepare

        def rejecting(name, require_quota=True):
            if name == 'first':
                raise SignInRequired(sign_in_required('first'))
            return prepare(name, require_quota)
        self.pool.prepare = rejecting
        await self.bridge.on_client({'id': 1, 'method': 'turn/start', 'params': {'threadId': 't', 'input': []}})
        self.assertEqual(self.bridge.current, 'second')
        # A dead login is not a failed quota read: logging one would misname the cause.
        self.assertNotIn('quota-check-failed', [line.split(' ')[1] for line in self.log_lines()])
        self.assertIsNone(self.state()['lastFailure'])

    async def test_failed_refresh_records_reason(self):
        await self.bridge.refresh({'id': 6, 'params': {'previousAccountId': 'different'}})
        self.assertEqual((self.state()['event'], self.state()['reason']), ('refresh-failed', 'refresh must keep the current account'))
        self.assertTrue(self.log_lines()[-1].endswith(
            ' refresh-failed account="first" reason="refresh must keep the current account" exc="LiveError"'))


class BridgeLogFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / BRIDGE_LOG_NAME

    def test_log_is_private_regardless_of_umask_and_appends(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.assertTrue(append_log_line(self.path, 'one'))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.path.chmod(0o644)  # a widened file is narrowed again on the next write
        self.assertTrue(append_log_line(self.path, 'two'))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.read_text(), 'one\ntwo\n')

    def test_symlink_or_directory_is_never_written_through(self):
        target = Path(self.temporary.name) / 'elsewhere'
        target.write_text('keep')
        self.path.symlink_to(target)
        self.assertFalse(append_log_line(self.path, 'line'))
        self.assertEqual(target.read_text(), 'keep')
        self.path.unlink()
        self.path.mkdir()
        self.assertFalse(append_log_line(self.path, 'line'))

    def test_cap_keeps_only_the_newest_lines(self):
        # 8-byte lines, a 64-byte cap: the 9th write compacts to the newest 3 lines first.
        with patch('xswap_live.BRIDGE_LOG_MAX_BYTES', 64), patch('xswap_live.BRIDGE_LOG_KEEP_LINES', 3):
            for index in range(12):
                self.assertTrue(append_log_line(self.path, f'line-{index:02d}'))
        self.assertEqual(self.path.read_text().splitlines(), [f'line-{index:02d}' for index in range(5, 12)])
        self.assertLessEqual(self.path.stat().st_size, 64)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in Path(self.temporary.name).iterdir()], [BRIDGE_LOG_NAME])  # no temp file left


class RunFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopped_status_and_bridge_carry_the_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            bridge = Bridge(Pool(), [sys.executable, '-c', 'pass'], {}, status_path=run_dir / 'status.json')
            bridge.status_log = lambda event: None

            async def idle():
                await asyncio.Event().wait()
            bridge.client_reader = idle
            bridge.manual_switch_reader = idle
            with self.assertRaises(LiveError):
                await bridge.run()
            state = json.loads((run_dir / 'status.json').read_text())
            log = (run_dir / BRIDGE_LOG_NAME).read_text().splitlines()
        self.assertEqual((state['event'], state['reason']), ('stopped', 'app-server exited'))
        # A bridge that dies after a failure must not overwrite that failure with its own exit.
        self.assertIsNone(state['lastFailure'])
        self.assertEqual(bridge.failure, 'app-server exited')
        self.assertTrue(log[-1].endswith(' stopped account="first" reason="app-server exited" exc="LiveError"'), log[-1])


class StatusRecordTests(unittest.TestCase):
    def test_status_record_carries_installed_version_and_persists_quota_known(self):
        # 0.7.5 wrote a hard-coded bridgeVersion and dropped quotaKnown after 'ready' (INT-5085).
        from codex_swap import __version__
        import json, pathlib, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'status.json'
            bridge = Bridge(Pool(), [sys.executable, '-c', 'pass'], {}, status_path=path)
            bridge.quota_known = True
            bridge.status('policy-applied')
            state = json.loads(path.read_text())
        self.assertEqual(state['bridgeVersion'], __version__)
        self.assertIs(state['quotaKnown'], True)
        self.assertEqual(state['event'], 'policy-applied')

    def test_status_record_names_conversation_home_and_pool(self):
        # list/doctor/upgrade build the reopen hint from these three fields (item 6);
        # a plain (desktop) Bridge has no conversation to name, so it writes null.
        import json, pathlib, tempfile
        thread = '00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'status.json'
            env = {'CODEX_HOME': tmp, 'OPENAI_API_KEY': 'must-not-leak'}
            bridge = Bridge(Pool(), [sys.executable, '-c', 'pass'], env, status_path=path)
            bridge.status('ready')
            first = json.loads(path.read_text())
            bridge.resume_thread = thread
            bridge.status('policy-applied')
            text = path.read_text()
            later = json.loads(text)
        self.assertIsNone(first['conversationId'])
        self.assertEqual(first['codexHome'], tmp)
        self.assertEqual(first['accounts'], ['first', 'second'])
        self.assertEqual(later['conversationId'], thread)
        self.assertNotIn('must-not-leak', text)


async def test_setup(case):
    await BridgeTests.asyncSetUp(case)
