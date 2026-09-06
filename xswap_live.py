"""Opt-in stdio bridge: keep one app-server/thread store while rotating auth.

No traffic proxying to OpenAI, token files, application patching, or process
restarts. Only documented JSON-RPC messages go to a child owned by this bridge.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
import uuid

from xswap_usage import normalize_limits, read_limits
from xswap_credentials import CredentialError, read_auth


class LiveError(Exception):
    pass


def jwt_claims(token):
    try:
        part = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def load_credentials(home):
    """Read the original credential store; never write or return raw errors."""
    try:
        data = read_auth(home)
        tokens = data.get('tokens') or {}
        token = tokens.get('access_token')
        account = tokens.get('account_id')
        if data.get('OPENAI_API_KEY') or not isinstance(token, str) or not token:
            raise LiveError('auto mode requires a signed-in ChatGPT account')
        claims = jwt_claims(token)
        auth = claims.get('https://api.openai.com/auth') or {}
        account = account or auth.get('chatgpt_account_id')
        if not isinstance(account, str) or not account:
            raise LiveError('ChatGPT account identity is missing')
        expiry = claims.get('exp')
        if not isinstance(expiry, (int, float)) or expiry <= time.time() + 30:
            raise LiveError('ChatGPT access token needs refresh or sign-in')
        return {'accessToken': token, 'chatgptAccountId': account,
                'chatgptPlanType': auth.get('chatgpt_plan_type')}
    except CredentialError as exc:
        raise LiveError(str(exc)) from None
    except (OSError, ValueError, TypeError, AttributeError):
        raise LiveError('cannot read ChatGPT credentials') from None


def validate_threshold(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value < 100:
        raise LiveError('weekly remaining threshold must be a number from 0 to less than 100')
    return float(value)


def quota_available(raw, model=None, weekly_remaining=0):
    """Unknown is not available. Require all applicable known windows > 0."""
    weekly_remaining = validate_threshold(weekly_remaining)
    buckets = normalize_limits(raw)
    applicable = [b for b in buckets if b['id'] == 'codex']
    if model and 'spark' in model.lower():
        applicable.extend(b for b in buckets if 'spark' in (b['id'] + b['name']).lower())
        if len(applicable) < 2:
            return None
    if not applicable:
        return None
    unknown = False
    for bucket in applicable:
        if bucket['reached']:
            return False
        if not bucket['windows']:
            unknown = True
        weekly_seen = False
        for window in bucket['windows']:
            left = window['remainingPercent']
            if left is None:
                unknown = True
            elif left <= 0:
                return False
            if window['windowMinutes'] == 10080:
                weekly_seen = True
                if left is not None and left <= weekly_remaining:
                    return False
        if weekly_remaining > 0 and not weekly_seen:
            unknown = True
    return None if unknown else True


def usage_failure(turn):
    return (turn.get('status') == 'failed' and
            (turn.get('error') or {}).get('codexErrorInfo') == 'usageLimitExceeded')


class AccountPool:
    def __init__(self, manager, names, codex):
        from codex_swap import check_file_store, validate_name
        if len(names) < 2 or len(set(names)) != len(names):
            raise LiveError('provide at least two distinct accounts with --accounts first,second')
        self.manager, self.codex = manager, codex
        self.homes = {}
        data = manager.read()
        for name in names:
            validate_name(name)
            _, home = manager.account(name)
            if data["accounts"].get(name, {}).get("disabled"):
                raise LiveError(f'account {name} is disabled; run: xswap enable {name}')
            check_file_store(home)
            self.homes[name] = home
        self.names = names

    @property
    def weekly_remaining(self):
        from xswap_cli import read_settings
        return validate_threshold(read_settings(self.manager).get('weeklyRemainingThreshold', 0))

    def prepare(self, name):
        home = self.homes[name]
        # Reject unsafe files before the official CLI can read/refresh them.
        try:
            read_auth(home)
        except CredentialError as exc:
            raise LiveError(str(exc)) from None
        # The official CLI refreshes its own source credentials if required.
        raw = read_limits(self.codex, self.manager.env(home), timeout=8)
        return load_credentials(home), raw

    def refresh(self, name):
        credentials, _ = self.prepare(name)
        return credentials


class Bridge:
    def __init__(self, pool, argv, env, emit=None, status_path=None):
        self.pool, self.argv, self.env = pool, argv, env
        self.emit = emit or self.stdout_message
        self.status_path = status_path
        self.process = None
        self.requests = {}
        self.client_requests = {}
        self.request_prefix = 'xswap-' + uuid.uuid4().hex + '-'
        self.counter = 0
        self.booting = True
        self.initialized = False
        self.active = set()
        self.starts = {}
        self.failed = {}
        self.attempted = {}
        self.attempted_ids = {}
        self.current = pool.names[0]
        self.current_id = None
        self.checked_at = 0
        self.checked_model = None
        self.last_quota = None
        self.gate = asyncio.Lock()
        self.tasks = set()
        self.stopping = False
        self.switches = 0
        self.policy_threshold = None

    @staticmethod
    def stdout_message(message):
        sys.stdout.write(json.dumps(message, separators=(',', ':')) + '\n')
        sys.stdout.flush()

    def status(self, event, **extra):
        # Whitelist operational metadata only. No prompts, tokens, raw RPC errors.
        if self.status_path:
            from codex_swap import atomic_json
            atomic_json(self.status_path, {'bridgePid': os.getpid(),
                'serverPid': self.process.pid if self.process else None,
                'cliPid': getattr(self, 'client_pid', None),
                'account': self.current, 'event': event, 'switches': self.switches,
                'bridgeVersion': '0.4.3', 'weeklyRemainingThreshold': self.threshold(),
                'updatedAt': time.time(), **extra})
        print(f'xswap auto: {event} ({self.current})', file=sys.stderr, flush=True)

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()

    async def rpc(self, method, params, timeout=20):
        self.counter += 1
        key = self.request_prefix + str(self.counter)
        future = asyncio.get_running_loop().create_future()
        self.requests[key] = future
        try:
            await self.send({'id': key, 'method': method, 'params': params})
            response = await asyncio.wait_for(future, timeout)
            if 'error' in response:
                raise LiveError('Codex rejected ' + method)
            return response.get('result', {})
        finally:
            self.requests.pop(key, None)

    async def install(self, name, credentials, raw=None):
        if self.active:
            raise LiveError('cannot change authentication while turns are active')
        await self.rpc('account/login/start', {'type': 'chatgptAuthTokens', **credentials})
        changed = self.current_id is not None and self.current_id != credentials['chatgptAccountId']
        self.current, self.current_id = name, credentials['chatgptAccountId']
        self.switches += int(changed)
        self.checked_at = time.monotonic()
        self.last_quota = raw
        self.status('switched' if changed else 'ready')

    def threshold(self):
        return validate_threshold(getattr(self.pool, 'weekly_remaining', 0))

    async def choose(self, excluded, model=None, excluded_ids=None):
        seen_ids = set(excluded_ids or ())
        if excluded:
            seen_ids.add(self.current_id)
        for name in self.pool.names:
            if name in excluded:
                continue
            try:
                credentials, raw = await asyncio.to_thread(self.pool.prepare, name)
                if credentials['chatgptAccountId'] in seen_ids:
                    continue
                seen_ids.add(credentials['chatgptAccountId'])
                if quota_available(raw, model, self.threshold()) is not True:
                    continue
                await self.install(name, credentials, raw)
                self.checked_model = model
                return True
            except Exception:
                self.status('candidate-unavailable', candidate=name)
        self.status('no-available-account')
        return False

    async def before_turn(self, model=None):
        if self.active:
            return
        threshold = self.threshold()
        if self.policy_threshold != threshold:
            self.policy_threshold = threshold
            self.status('policy-applied')
        if time.monotonic() - self.checked_at < 30 and model == self.checked_model:
            available = quota_available(self.last_quota or {}, model, self.threshold())
        else:
            try:
                _, raw = await asyncio.to_thread(self.pool.prepare, self.current)
                self.last_quota, self.checked_at, self.checked_model = raw, time.monotonic(), model
                available = quota_available(raw, model, self.threshold())
            except Exception:
                available = None
        if available is False:
            await self.choose({self.current}, model)

    def schedule(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def refresh(self, message):
        try:
            previous = (message.get('params') or {}).get('previousAccountId')
            credentials = await asyncio.wait_for(asyncio.to_thread(self.pool.refresh, self.current), 9)
            if credentials['chatgptAccountId'] != self.current_id or (previous and previous != self.current_id):
                raise LiveError('refresh must keep the current account')
            await self.send({'id': message['id'], 'result': credentials})
        except Exception:
            await self.send({'id': message['id'], 'error': {'code': -32000,
                             'message': 'xswap: re-authenticate the active source account'}})
            self.status('refresh-failed')

    async def on_server(self, message):
        key = message.get('id')
        if key in self.requests and 'method' not in message:
            if not self.requests[key].done():
                self.requests[key].set_result(message)
            return
        method = message.get('method')
        params = message.get('params') or {}
        if method == 'account/chatgptAuthTokens/refresh':
            self.schedule(self.refresh(message))
            return
        if self.booting:
            return
        if key in self.client_requests and 'method' not in message:
            thread = self.client_requests.pop(key)
            if 'error' in message:
                self.active.discard(thread)
        if method == 'account/rateLimits/updated':
            self.last_quota = params
            self.checked_at = time.monotonic()
        elif method == 'turn/started':
            self.active.add(params['threadId'])
        elif method == 'turn/completed':
            thread = params['threadId']
            self.active.discard(thread)
            if usage_failure(params.get('turn') or {}) and thread in self.starts:
                self.failed[thread] = self.starts[thread]
                self.attempted.setdefault(thread, set()).add(self.current)
                self.attempted_ids.setdefault(thread, set()).add(self.current_id)
            else:
                self.failed.pop(thread, None)
                self.attempted.pop(thread, None)
                self.attempted_ids.pop(thread, None)
        self.emit(message)
        if method == 'turn/completed' and self.failed:
            self.schedule(self.continue_failed())

    async def continue_failed(self):
        async with self.gate:
            if self.stopping or self.active:
                return
            for thread, original in list(self.failed.items()):
                if thread not in self.failed:
                    continue
                tried = self.attempted.setdefault(thread, {self.current})
                tried_ids = self.attempted_ids.setdefault(thread, {self.current_id})
                if self.current not in tried and self.current_id not in tried_ids and quota_available(self.last_quota or {}, original.get('model'), self.threshold()) is True:
                    ready = True
                else:
                    ready = await self.choose(tried, original.get('model'), tried_ids)
                # Interrupts/new user messages may have arrived during quota lookup.
                if thread not in self.failed or self.stopping:
                    return
                self.failed.pop(thread, None)
                if not ready:
                    continue
                tried.add(self.current)
                tried_ids.add(self.current_id)
                params = dict(original)
                params['input'] = [{'type': 'text', 'text':
                    'Continue the previous task from its saved state after the usage-limit interruption. '
                    'Check completed actions and tool results; do not repeat completed side effects.'}]
                self.active.add(thread)
                try:
                    await self.rpc('turn/start', params)
                    self.status('continued-after-limit')
                except Exception:
                    self.active.discard(thread)
                    self.status('continuation-failed')
                # One continuation at a time; other failures wait for its completion.
                return

    async def on_client(self, message):
        method = message.get('method')
        params = message.get('params') or {}
        if method == 'initialize':
            if self.initialized:
                raise LiveError('duplicate initialization')
            params = dict(params)
            params['capabilities'] = {**(params.get('capabilities') or {}), 'experimentalApi': True}
            result = await self.rpc('initialize', params)
            await self.send({'method': 'initialized', 'params': {}})
            credentials, raw = await asyncio.to_thread(self.pool.prepare, self.current)
            await self.install(self.current, credentials, raw)
            self.booting, self.initialized = False, True
            self.emit({'id': message['id'], 'result': result})
            return
        if method == 'initialized':
            return
        if not self.initialized:
            raise LiveError('initialize is required')
        if method in ('account/login/start', 'account/logout', 'account/login/cancel'):
            self.emit({'id': message['id'], 'error': {'code': -32600,
                'message': 'xswap auto owns authentication. Use xswap add for login or a normal xswap app window.'}})
            return
        if method == 'turn/interrupt':
            self.failed.pop(params.get('threadId'), None)
            self.starts.pop(params.get('threadId'), None)
            await self.send(message)
            return
        if method == 'turn/start':
            thread = params['threadId']
            self.failed.pop(thread, None)
            self.attempted.pop(thread, None)
            self.attempted_ids.pop(thread, None)
            async with self.gate:
                await self.before_turn(params.get('model'))
                self.starts[thread] = dict(params)
                self.active.add(thread)
                self.client_requests[message['id']] = thread
                await self.send(message)
            return
        await self.send(message)

    async def server_reader(self):
        while line := await self.process.stdout.readline():
            try:
                await self.on_server(json.loads(line))
            except (ValueError, TypeError, KeyError):
                raise LiveError('invalid app-server protocol message') from None
        raise LiveError('app-server exited')

    async def client_reader(self):
        reader = asyncio.StreamReader(limit=32 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        try:
            while line := await reader.readline():
                await self.on_client(json.loads(line))
        finally:
            transport.close()

    async def run(self):
        self.process = await asyncio.create_subprocess_exec(*self.argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=self.env, start_new_session=True, limit=32 * 1024 * 1024)
        readers = [asyncio.create_task(self.server_reader()), asyncio.create_task(self.client_reader())]
        try:
            done, _ = await asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.stopping = True
            for task in [*readers, *self.tasks]:
                task.cancel()
            await asyncio.gather(*readers, *self.tasks, return_exceptions=True)
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
            self.status('stopped')


def proxy_main():
    from codex_swap import Manager, SwapError
    real = os.environ.get('XSWAP_REAL_CODEX')
    if not real or not Path(real).is_absolute() or not Path(real).is_file():
        print('xswap auto: real Codex executable is missing', file=sys.stderr)
        return 1
    args = sys.argv[1:]
    if 'app-server' not in args:
        os.execve(real, [real, *args], {k: v for k, v in os.environ.items() if k != 'CODEX_CLI_PATH'})
    # The wrapper is a stdio bridge, never a network listener or daemon manager.
    if any(arg in args for arg in ('daemon', 'proxy', 'generate-ts', 'generate-json-schema')):
        os.execve(real, [real, *args], dict(os.environ))
    try:
        manager = Manager()
        names = os.environ.get('XSWAP_ACCOUNTS', '').split(',')
        pool = AccountPool(manager, names, real)
        runtime = Path(os.environ['CODEX_HOME'])
        if runtime != manager.root / 'auto' / 'codex':
            raise LiveError('auto mode needs its dedicated runtime home')
        lock = os.open(runtime.parent / '.bridge.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock)
            raise LiveError('an auto-mode app server is already running') from None
        env = manager.env(runtime)
        for key in list(env):
            if key.startswith('XSWAP_') or key in ('CODEX_CLI_PATH', 'CODEX_APP_SERVER_WS_URL', 'CODEX_APP_SERVER_USE_LOCAL_DAEMON'):
                env.pop(key, None)
        try:
            asyncio.run(Bridge(pool, [real, *args], env, status_path=runtime.parent / 'status.json').run())
        finally:
            os.close(lock)
        return 0
    except (LiveError, SwapError, OSError, ValueError, asyncio.TimeoutError):
        print('xswap auto: bridge stopped; check source account login and CLI compatibility. Original account stores were not replaced.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
