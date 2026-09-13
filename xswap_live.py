"""Opt-in stdio bridge: keep one app-server/thread store while rotating auth.

No traffic proxying to OpenAI, token files, application patching, or process
restarts. Only documented JSON-RPC messages go to a child owned by this bridge.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time
import uuid

from xswap_usage import UsageError, clean, is_sign_in_failure, normalize_limits, read_limits
from xswap_credentials import CredentialError, read_auth


class LiveError(Exception):
    pass


class SignInRequired(LiveError):
    """The usage service rejected this account's login (recorded in auth-state.json).

    Raised by AccountPool.prepare instead of UsageError so the bridge can tell a dead
    login (skip it, move away from it) from a usage-service outage (tolerate it). The
    message is a hand-written constant: safe for status.json and the disconnect line.
    """


def sign_in_required(name):
    return f'sign-in required; run: xswap login {name}'


BRIDGE_LOG_NAME = 'bridge.log'
BRIDGE_LOG_MAX_BYTES = 1024 * 1024
BRIDGE_LOG_KEEP_LINES = 1000
# Events whose `reason` describes a failure. The newest one persists as `lastFailure`
# in status.json until the bridge exits: on 2026-09-10 three manual switches ended as
# manualState "failed" with reason null, and `stopped` had overwritten the event.
# `stopped` itself is deliberately absent -- a bridge that dies after a failed switch
# would otherwise replace that cause with 'app-server exited'. Its own per-event
# `reason` is still written, and bridge.log keeps the whole order.
FAILURE_EVENTS = frozenset({'manual-switch-failed', 'candidate-unavailable', 'no-available-account',
                            'continuation-failed', 'refresh-failed', 'quota-check-failed'})


def failure_reason(exc):
    """Curated, non-secret one-liner for status.json, bridge.log, and the disconnect message.

    LiveError/UsageError carry hand-written constants only. SwapError and OSError
    are classified without their paths. Anything else may wrap RPC payloads or
    paths, so expose just the exception type.
    """
    if isinstance(exc, (LiveError, UsageError)):
        return str(exc)
    # TimeoutError is an OSError subclass, so this branch has to stay above that one.
    if isinstance(exc, asyncio.TimeoutError):
        return 'app-server request timed out'
    from codex_swap import SwapError
    if isinstance(exc, SwapError):
        text = str(exc)
        if 'No matching account' in text:
            return 'account is not registered'
        if 'config.toml' in text:
            return 'account config.toml is unreadable'
        if 'credential storage is' in text:
            return 'account uses non-file credential storage'
        return 'account store is unusable (xswap doctor)'
    if isinstance(exc, OSError):
        return 'local I/O error: ' + (exc.strerror or type(exc).__name__)
    return type(exc).__name__


class IdentityMismatch(LiveError):
    """The app server reports a different login than the one just sent to it."""

    def __init__(self):
        super().__init__('identity mismatch')


def append_log_line(path, line):
    """Append one line to a private bridge log; never raise (logging must not stop a bridge).

    Only a user-owned regular file is written through (O_NOFOLLOW plus an fstat
    check), created 0600 and narrowed back to 0600 if widened. O_NONBLOCK is what
    makes that check reachable: without it a non-regular blocking file at this path
    (a FIFO) freezes the open itself, and status() runs on the bridge's event loop. When the file would
    exceed BRIDGE_LOG_MAX_BYTES it is first compacted to its newest
    BRIDGE_LOG_KEEP_LINES lines. The bridge holding the run dir's .bridge.lock is
    the only writer, so the compaction needs no lock of its own.
    """
    data = (line + '\n').encode()
    try:
        compact_log(path, len(data))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return False
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            os.write(fd, data)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def compact_log(path, incoming):
    """Rewrite the log with its newest lines when `incoming` more bytes would pass the cap."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_size + incoming <= BRIDGE_LOG_MAX_BYTES):
            return
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            lines = stream.read().splitlines(keepends=True)
    finally:
        os.close(fd)
    tail_fd, tmp = tempfile.mkstemp(prefix='.bridge-log-', dir=path.parent)
    try:
        with os.fdopen(tail_fd, 'wb') as stream:
            stream.write(b''.join(lines[-BRIDGE_LOG_KEEP_LINES:]))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def jwt_claims(token):
    try:
        part = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def token_emails(token):
    """Email labels a ChatGPT access token carries, for comparison with account/read.

    Real access tokens keep the address under the profile namespace; id tokens
    and test fixtures keep it at the top level. Case-folded; may be empty.
    """
    claims = jwt_claims(token)
    profile = claims.get('https://api.openai.com/profile')
    candidates = [claims.get('email'), profile.get('email') if isinstance(profile, dict) else None]
    return {value.casefold() for value in candidates if isinstance(value, str) and value}


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


def buckets_available(buckets, model=None, weekly_remaining=0):
    """Same rule as quota_available, but on already-normalized buckets."""
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


def quota_available(raw, model=None, weekly_remaining=0):
    """Unknown is not available. Require all applicable known windows > 0."""
    weekly_remaining = validate_threshold(weekly_remaining)
    return buckets_available(normalize_limits(raw), model, weekly_remaining)


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
        enabled_names = {n for n, _ in manager.enabled_accounts()}
        for name in names:
            validate_name(name)
            _, home = manager.account(name)
            if name not in enabled_names:
                raise LiveError(f'account {name} is disabled; run: xswap enable {name}')
            check_file_store(home)
            self.homes[name] = home
        self.names = names

    @property
    def weekly_remaining(self):
        from xswap_cli import read_settings
        return validate_threshold(read_settings(self.manager).get('weeklyRemainingThreshold', 0))

    def prepare(self, name, require_quota=True):
        # Manual selections may be outside the automatic fallback pool.
        from codex_swap import check_file_store, identity
        if name not in {n for n, _ in self.manager.enabled_accounts()}:
            raise LiveError('selected account is disabled or removed')
        _, home = self.manager.account(name)
        check_file_store(home)
        # Reject unsafe files before the official CLI can read/refresh them.
        try:
            read_auth(home)
        except CredentialError as exc:
            raise LiveError(str(exc)) from None
        # A login the usage service already rejected (auth-state.json) is not probed
        # again by the pool; `xswap login NAME` or a successful live `xswap list` clears it.
        label = identity(home)
        if self.manager.auth_failure(name, label) is not None:
            raise SignInRequired(sign_in_required(name))
        # The official CLI refreshes its own source credentials if required.
        try:
            raw = read_limits(self.codex, self.manager.env(home), timeout=8)
        except UsageError as exc:
            if is_sign_in_failure(exc):
                self.manager.remember_auth_failure(name, label)
                raise SignInRequired(sign_in_required(name)) from None
            # Credentials are already validated; a usage-service outage is not
            # a login problem. Callers that can re-check later may continue.
            if require_quota:
                raise
            raw = None
        else:
            self.manager.clear_auth_failure(name)
        return load_credentials(home), raw

    def refresh(self, name):
        credentials, _ = self.prepare(name)
        return credentials

    def credentials(self, name):
        """Tokens only, for putting a login back after a failed verification: no usage read."""
        from codex_swap import check_file_store
        if name not in {n for n, _ in self.manager.enabled_accounts()}:
            raise LiveError('selected account is disabled or removed')
        _, home = self.manager.account(name)
        check_file_store(home)
        return load_credentials(home)

    def first_available(self):
        """The first pool member whose current login is not recorded as rejected; the
        pool's first name when every member is (prepare then reports the reason)."""
        from codex_swap import SwapError, identity
        for name in self.names:
            try:
                label = identity(self.homes[name])
            except SwapError:
                continue
            if self.manager.auth_failure(name, label) is None:
                return name
        return self.names[0]


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
        self.instance = uuid.uuid4().hex
        self.manual_request = None
        self.manual_state = None
        self.quota_known = None  # Persisted in every status write, not only 'ready'.
        self.failure = None
        self.manual_reason = None  # Why the manual request is pending or failed; persists like manual_state.
        self.last_failure = None  # Newest failure {event, account, reason, at[, candidate]}; persists until exit.
        # What the app server itself answered to account/read after the last login
        # it was sent (0.8.0); `current` alone is what this bridge believes it installed.
        self.verified_account = None
        self.verified_identity = None
        self.verified_at = None
        self.verify_reason = None

    @staticmethod
    def stdout_message(message):
        sys.stdout.write(json.dumps(message, separators=(',', ':')) + '\n')
        sys.stdout.flush()

    def status(self, event, exc=None, **extra):
        # Whitelist operational metadata only. No prompts, tokens, raw RPC errors.
        # `exc` is the exception behind a failure event: its classified reason goes
        # into the record and the log; its type goes into the log only.
        # conversationId/codexHome/accounts let `xswap list`, `doctor`, and `upgrade`
        # name the command that reopens this session on newer code: on 2026-09-10 three
        # 0.7.2 bridges ran next to an installed 0.7.6 and no reader could say how.
        if exc is not None:
            extra['reason'] = failure_reason(exc)
        if event in FAILURE_EVENTS and extra.get('reason'):
            self.last_failure = {'event': event, 'account': self.current, 'reason': extra['reason'],
                                 'at': time.time(), **({'candidate': extra['candidate']} if 'candidate' in extra else {})}
        if self.status_path:
            from codex_swap import __version__, atomic_json
            atomic_json(self.status_path, {'bridgePid': os.getpid(),
                'serverPid': self.process.pid if self.process else None,
                'cliPid': getattr(self, 'client_pid', None),
                'account': self.current, 'event': event, 'switches': self.switches,
                'bridgeVersion': __version__, 'weeklyRemainingThreshold': self.threshold(),
                'quotaKnown': self.quota_known,
                'manualSwitchVersion': 1, 'bridgeInstance': self.instance,
                'manualRequest': self.manual_request, 'manualState': self.manual_state,
                'manualReason': self.manual_reason, 'lastFailure': self.last_failure,
                'conversationId': getattr(self, 'resume_thread', None),
                'codexHome': (self.env or {}).get('CODEX_HOME'),
                'accounts': list(self.pool.names),
                'verifiedAccount': self.verified_account, 'verifiedIdentity': self.verified_identity,
                'verifiedAt': self.verified_at, 'verifyReason': self.verify_reason,
                'updatedAt': time.time(), **extra})
            self.log(event, extra, type(exc).__name__ if exc is not None else None)
        self.status_log(event)

    def log(self, event, fields, exc_type=None):
        """One bridge.log line per event: local time, event, account, the event's extras, exception type."""
        parts = [datetime.now().astimezone().isoformat(timespec='milliseconds'), event,
                 'account=' + json.dumps(self.current)]
        parts.extend(f'{key}={json.dumps(value, default=str)}' for key, value in fields.items())
        if exc_type:
            parts.append('exc=' + json.dumps(exc_type))
        append_log_line(self.status_path.parent / BRIDGE_LOG_NAME, ' '.join(parts))

    def status_log(self, event):
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
        try:
            label, reason = await self.verify_identity(credentials)
        except IdentityMismatch:
            # The server holds some other login now; put the account this bridge
            # already trusted back, then record the outcome for the failed switch.
            await self.restore_current()
            self.status('identity-mismatch', candidate=name)
            raise
        changed = self.current_id is not None and self.current_id != credentials['chatgptAccountId']
        self.current, self.current_id = name, credentials['chatgptAccountId']
        self.record_verification(name if label else None, label, reason)
        self.switches += int(changed)
        # Unknown quota: leave checked_at at 0 so before_turn re-reads it first.
        self.checked_at = time.monotonic() if raw is not None else 0
        self.last_quota = raw
        self.quota_known = raw is not None
        self.status('switched' if changed else 'ready', quotaKnown=self.quota_known)

    def record_verification(self, account, label, reason):
        self.verified_account = account
        self.verified_identity = label
        self.verified_at = time.time() if account else None
        self.verify_reason = reason

    async def verify_identity(self, credentials):
        """Read back which login the app server holds after account/login/start.

        account/read is local (no refresh, no network) and answers with the email
        of the token the server actually installed. Returns (label, None) when it
        names the token just sent, (None, reason) when the answer cannot settle it,
        and raises IdentityMismatch when the server holds no login or another one.
        2026-09-10: a session's record named the account the bridge had asked for,
        and codex-cli 0.154.0 leaves the previous login installed when an
        external-token login is rejected. Plan types (account/updated, rate limits)
        are shared by every account on a plan and are never treated as identity.
        """
        expected = token_emails(credentials['accessToken'])
        try:
            result = await self.rpc('account/read', {}, timeout=10)
        except TimeoutError:
            return None, 'app-server request timed out'
        except LiveError:
            return None, 'account/read rejected'
        if not isinstance(result, dict) or 'account' not in result:
            return None, 'unrecognized account/read response'
        account = result['account']
        if not isinstance(account, dict) or account.get('type') != 'chatgpt':
            raise IdentityMismatch()  # no login at all, an API key, or another provider
        if not expected:
            return None, 'no email claim'
        email = account.get('email')
        if not isinstance(email, str) or email.casefold() not in expected:
            raise IdentityMismatch()
        return clean(email), None

    async def restore_current(self):
        """Best effort after a mismatch: re-send the trusted account's tokens and re-check."""
        if self.current_id is None:
            self.record_verification(None, None, 'identity mismatch')  # nothing was installed before
            return
        try:
            credentials = await asyncio.to_thread(self.pool.credentials, self.current)
            await self.rpc('account/login/start', {'type': 'chatgptAuthTokens', **credentials})
            label, reason = await self.verify_identity(credentials)
        except Exception:
            self.record_verification(None, None, 'identity unknown after rollback')
            self.status('identity-unknown')
            return
        self.record_verification(self.current if label else None, label, reason or 'identity mismatch')

    async def apply_manual_switch(self):
        """Called with gate held; only idle servers may change authentication."""
        if not self.status_path or not self.initialized or self.stopping:
            return False
        from xswap_switch import read_private_json
        try:
            request = read_private_json(self.status_path.parent / 'switch.json')
        except (OSError, ValueError):
            return False
        if (request.get('instance') != self.instance or
                not isinstance(request.get('id'), str) or
                not isinstance(request.get('account'), str)):
            return False
        if request['id'] == self.manual_request and self.manual_state != 'pending':
            return False
        new_request = request['id'] != self.manual_request
        self.manual_request = request['id']
        # The requested name is validated by prepare(); until then treat it as text.
        candidate = clean(request['account'])
        if self.active:
            if new_request or self.manual_state != 'pending':
                self.manual_state = 'pending'
                self.manual_reason = f'waiting for {len(self.active)} active turn(s) to finish'
                self.status('manual-switch-pending', candidate=candidate, reason=self.manual_reason)
            return False
        self.manual_state, self.manual_reason = 'applying', None
        self.status('manual-switch-applying', candidate=candidate)
        try:
            credentials, raw = await asyncio.to_thread(self.pool.prepare, request['account'])
            await self.install(request['account'], credentials, raw)
            self.manual_state = 'applied'
            self.status('manual-switch-applied')
            # Update the TUI's quota cache for the newly authenticated account.
            self.emit({'method': 'account/rateLimits/updated', 'params': raw})
            return True
        except Exception as exc:
            self.manual_state, self.manual_reason = 'failed', failure_reason(exc)
            self.status('manual-switch-failed', exc=exc, candidate=candidate)
            return False

    async def manual_switch_reader(self):
        while True:
            async with self.gate:
                await self.apply_manual_switch()
            await asyncio.sleep(0.25)

    def threshold(self):
        return validate_threshold(getattr(self.pool, 'weekly_remaining', 0))

    async def choose(self, excluded, model=None, excluded_ids=None):
        seen_ids = set(excluded_ids or ())
        if excluded:
            seen_ids.add(self.current_id)
        outcomes = []
        for name in self.pool.names:
            if name in excluded:
                continue
            try:
                credentials, raw = await asyncio.to_thread(self.pool.prepare, name)
                if credentials['chatgptAccountId'] in seen_ids:
                    outcomes.append((name, 'same identity as an excluded account'))
                    self.status('candidate-skipped', candidate=name, reason=outcomes[-1][1])
                    continue
                seen_ids.add(credentials['chatgptAccountId'])
                available = quota_available(raw, model, self.threshold())
                if available is not True:
                    outcomes.append((name, 'quota unknown' if available is None else 'no quota above the weekly reserve'))
                    self.status('candidate-skipped', candidate=name, reason=outcomes[-1][1])
                    continue
                await self.install(name, credentials, raw)
                self.checked_model = model
                return True
            except Exception as exc:
                outcomes.append((name, failure_reason(exc)))
                self.status('candidate-unavailable', exc=exc, candidate=name)
        self.status('no-available-account', reason='; '.join(f'{name}: {why}' for name, why in outcomes)
                    or 'every pool account was already tried')
        return False

    async def before_turn(self, model=None):
        if await self.apply_manual_switch():
            return
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
            except SignInRequired:
                # The service rejected the current login: move before this turn, not
                # after it fails with an auth error that no continuation can retry.
                # Not a 'quota-check-failed': nothing went wrong with the check.
                available = False
            except Exception as exc:
                self.status('quota-check-failed', exc=exc)
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
        except Exception as exc:
            await self.send({'id': message['id'], 'error': {'code': -32000,
                             'message': 'xswap: re-authenticate the active source account'}})
            self.status('refresh-failed', exc=exc)

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
                except Exception as exc:
                    self.active.discard(thread)
                    self.status('continuation-failed', exc=exc)
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
            # A usage-service outage at startup must not close the session;
            # quota is re-read before the first turn (2026-09-08 incident).
            # A login the service rejected is different: start on another pool
            # member instead of installing tokens the server will refuse.
            start = await asyncio.to_thread(self.pool.first_available)
            try:
                credentials, raw = await asyncio.to_thread(self.pool.prepare, start, require_quota=False)
                await self.install(start, credentials, raw)
            except SignInRequired:
                if not await self.choose({start}):
                    raise
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
        readers = [asyncio.create_task(self.server_reader()), asyncio.create_task(self.client_reader()),
                   asyncio.create_task(self.manual_switch_reader())]
        failure = None
        try:
            done, _ = await asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Exception as exc:
            failure = exc
            self.failure = failure_reason(exc)
            raise
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
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
            self.status('stopped', **({'exc': failure} if failure is not None else {}))


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
    except (TimeoutError, LiveError, SwapError, OSError, ValueError) as exc:
        reason = failure_reason(exc)
        print(f'xswap auto: bridge stopped ({reason}); check source account login and CLI compatibility. Original account stores were not replaced.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
