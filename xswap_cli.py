"""Connect the unmodified Codex TUI to a private local auto-auth app server."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import sys
import tempfile
import time
import uuid

from xswap_live import BRIDGE_LOG_NAME, AccountPool, Bridge, LiveError, validate_threshold


# `upgrade` is Codex's own name for the standalone updater and was in neither set, so
# interactive_args called it interactive: a wrapped `codex upgrade` ran inside the bridge,
# whose CODEX_HOME is auto/cli-codex, and installed the release into xswap's own state --
# the accident `xswap relocate-codex` exists to undo.
NON_INTERACTIVE = {'exec', 'e', 'review', 'login', 'logout', 'mcp', 'plugin', 'mcp-server',
    'app-server', 'remote-control', 'app', 'completion', 'update', 'upgrade', 'doctor',
    'sandbox', 'debug', 'apply', 'queue', 'archive', 'delete', 'migrate-rollouts',
    'unarchive', 'cloud', 'exec-server', 'features', 'help'}
VALUE_FLAGS = {'-c', '--config', '-C', '--cd', '-m', '--model', '-p', '--profile', '-s',
    '--sandbox', '-a', '--ask-for-approval', '--enable', '--disable', '-i', '--image',
    '--add-dir', '--local-provider', '--remote', '--remote-auth-token-env'}
# Pass-through subcommands that keep the caller's own Codex home. They manage the
# caller's sign-in, installation, shell, or desktop app rather than run a task as an
# account: `login`/`logout` write the caller's auth.json, `app` opens the Desktop app
# through `open` (the injected env would not reach it), `app-server` is spawned by the
# bridge/desktop/usage code with an explicit env, and `update`/`upgrade` install the next
# Codex release under $CODEX_HOME/packages/standalone/, which must never be a profile.
PASSTHROUGH_EXCLUDED = {'login', 'logout', 'app', 'app-server', 'completion', 'help',
    'update', 'upgrade'}


def interactive_args(args):
    if any(a in ('--help', '-h', '--version', '-V', '--remote') or a.startswith('--remote=') for a in args):
        return False
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in VALUE_FLAGS:
            skip = True
        elif not arg.startswith('-'):
            return arg not in NON_INTERACTIVE
    return True


def passthrough_subcommand(args):
    """The Codex subcommand a pass-through invocation runs as an xswap account, or None.

    None keeps the caller's own home: --help/--version/--remote forms, a bare
    invocation (interactive, so it never reaches pass-through while automatic
    switching is on), and PASSTHROUGH_EXCLUDED subcommands. Skips option values the
    same way interactive_args does, so `codex -m gpt-5 exec ...` resolves to `exec`.
    """
    if any(a in ('--help', '-h', '--version', '-V', '--remote') or a.startswith('--remote=') for a in args):
        return None
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in VALUE_FLAGS:
            skip = True
        elif not arg.startswith('-'):
            return None if arg in PASSTHROUGH_EXCLUDED else arg
    return None


def server_overrides(args):
    result = []
    iterator = iter(args)
    for arg in iterator:
        if arg in ('-c', '--config', '--enable', '--disable'):
            value = next(iterator, None)
            if value is None:
                raise LiveError('missing Codex option value')
            result.extend((arg, value))
        elif arg.startswith(('--config=', '--enable=', '--disable=')) or (arg.startswith('-c') and len(arg) > 2):
            result.append(arg)
        elif arg == '--strict-config':
            result.append(arg)
        elif arg in VALUE_FLAGS:
            next(iterator, None)
    return result


class BusyThreadError(LiveError):
    pass


def writer_busy(home, thread_id):
    """Probe an existing writer lock without deleting or modifying it."""
    try:
        value = str(uuid.UUID(thread_id))
        path = Path(home) / 'thread-writer-locks' / (value + '.lock')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (ValueError, TypeError, AttributeError, FileNotFoundError):
        return False
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def saved_thread(home, thread_id):
    for folder in ('sessions', 'archived_sessions'):
        if any((Path(home) / folder).glob(f'**/*-{thread_id}.jsonl')):
            return True
    return False


class WebSocketBridge(Bridge):
    def __init__(self, *args, socket, **kwargs):
        self.socket = socket
        self.outbox = asyncio.Queue(maxsize=4096)
        self.ready = asyncio.Event()
        self.initialize_result = None
        self.resume_thread = None
        self.thread_requests = set()
        self.list_requests = set()
        self.loaded_threads = set()
        self.resume_candidates = []
        super().__init__(*args, emit=self.outbox.put_nowait, **kwargs)
        self.picker_prefix = self.request_prefix + 'picker-'

    def status_log(self, event):
        # The foreground TUI owns the terminal, including stderr. Operational
        # status is still written by Bridge.status for xswap auto-status.
        pass

    async def rpc(self, method, params, timeout=20):
        result = await super().rpc(method, params, timeout)
        if method == 'initialize':
            self.initialize_result = result
        return result

    async def picker_client(self, socket):
        """A picker borrows the initialized server, with isolated request IDs.

        It may browse sessions but cannot start turns or change authentication.
        Closing a picker never closes the main TUI or its child server.
        """
        allowed = {'thread/list', 'thread/read', 'thread/loaded/list',
                   'config/read', 'configRequirements/read', 'account/read',
                   'account/rateLimits/read', 'model/list'}
        initialized = False
        async for text in socket:
            message = json.loads(text)
            method = message.get('method')
            if 'id' not in message:
                continue
            reply = {'id': message['id']}
            if method == 'initialize' and not initialized:
                await asyncio.wait_for(self.ready.wait(), 20)
                reply['result'] = self.initialize_result
                initialized = True
            elif not initialized or method not in allowed or self.stopping:
                reply['error'] = {'code': -32600, 'message': 'xswap: picker connection supports session browsing only'}
            else:
                # Use the bridge namespace so identical main/picker IDs cannot collide.
                self.counter += 1
                key = self.picker_prefix + str(self.counter)
                future = asyncio.get_running_loop().create_future()
                self.requests[key] = future
                try:
                    await self.send({**message, 'id': key})
                    result = await asyncio.wait_for(future, 20)
                    if method == 'thread/list':
                        result = self.available_threads(result)
                    reply.update({k: result[k] for k in ('result', 'error') if k in result})
                finally:
                    self.requests.pop(key, None)
            await socket.send(json.dumps(reply, separators=(',', ':')))

    async def on_client(self, message):
        method = message.get('method')
        if method == 'thread/list' and 'id' in message:
            self.list_requests.add(message['id'])
        if method in ('thread/start', 'thread/resume', 'thread/fork') and 'id' in message:
            self.thread_requests.add(message['id'])
        if method == 'turn/start':
            self.remember_thread((message.get('params') or {}).get('threadId'))
        await super().on_client(message)
        if method == 'initialize':
            self.ready.set()

    def remember_thread(self, value):
        try:
            thread = str(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError):
            return
        changed = thread != self.resume_thread
        self.resume_thread = thread
        self.resume_candidates = [thread, *[t for t in self.resume_candidates if t != thread]][:100]
        # Publish the conversation so list/doctor/upgrade can name the resume command
        # for this session. The first turn already lands in the 'policy-applied' write;
        # a later /resume to another thread must not keep advertising the old one.
        # Best effort only: the hint is never worth the session.
        if changed and self.initialized and not self.stopping and self.status_path:
            with contextlib.suppress(LiveError, OSError, ValueError):
                self.status('conversation-known')

    def available_threads(self, message):
        result = message.get('result')
        if not isinstance(result, dict) or not isinstance(result.get('data'), list):
            return message
        home = self.env.get('CODEX_HOME')
        if not home:
            return message
        rows = [row for row in result['data'] if row.get('id') in self.loaded_threads
                or not writer_busy(home, row.get('id'))]
        return {**message, 'result': {**result, 'data': rows}}

    async def on_server(self, message):
        key = message.get('id')
        if (isinstance(key, str) and key.startswith(self.picker_prefix)
                and 'method' not in message and key not in self.requests):
            return  # A timed-out picker response must never reach the main TUI.
        if key in self.list_requests and 'method' not in message:
            self.list_requests.discard(key)
            message = self.available_threads(message)
        if key in self.thread_requests and 'method' not in message:
            self.thread_requests.discard(key)
            if 'error' not in message:
                thread_id = ((message.get('result') or {}).get('thread') or {}).get('id')
                self.remember_thread(thread_id)
                self.loaded_threads.add(thread_id)
        await super().on_server(message)

    async def client_reader(self):
        async def receive():
            async for text in self.socket:
                await self.on_client(json.loads(text))

        async def write():
            while True:
                await self.socket.send(json.dumps(await self.outbox.get(), separators=(',', ':')))

        tasks = [asyncio.create_task(receive()), asyncio.create_task(write())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve_cli(pool, real, args, env, status_path, socket_path, bridge_class=WebSocketBridge):
    """One TUI and one child server. No TCP listener and no TUI restart."""
    from codex_swap import SwapError, private_dir
    from websockets.asyncio.server import unix_serve
    from websockets.exceptions import ConnectionClosed
    connected = False
    active_bridge = None
    client_ready = asyncio.Event()
    bridge_failed = False

    async def handle(socket):
        nonlocal connected, active_bridge, bridge_failed
        if connected:
            try:
                await active_bridge.picker_client(socket)
            except ConnectionClosed:
                pass
            except (TimeoutError, ValueError, LiveError):
                await socket.close(1008, 'picker connection failed')
            return
        connected = True
        active_bridge = bridge_class(pool, [real, 'app-server', '--stdio', *server_overrides(args)],
            env, socket=socket, status_path=status_path)
        await client_ready.wait()
        active_bridge.client_pid = cli.pid
        # The record is created before the TUI is spawned and stays empty until this point,
        # so a sweep that fires while the TUI is still starting (a cold release on a spun-up
        # volume, a loaded machine) classifies it `empty` and removes it. Recreate it rather
        # than letting the O_CREAT below kill the session with a bare FileNotFoundError.
        # Inside the failure path, not above it: private_dir raises SwapError for a directory
        # it will not trust and propagates the OSError a second sweep causes between its own
        # mkdir and its stat/chmod, and anything that escapes this handler leaves
        # bridge_failed False -- the silent death this recreate was added to remove.
        try:
            private_dir(status_path.parent)
            lock = os.open(status_path.parent / '.bridge.lock', os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
        except (SwapError, OSError) as error:
            active_bridge.failure = f'run record {status_path.parent} unusable ' \
                                    f'({getattr(error, "strerror", None) or type(error).__name__})'
            bridge_failed = True
            return
        try:
            await active_bridge.run()
        except Exception:
            # Avoid writing RPC payloads or credentials into the terminal/logs.
            bridge_failed = True
        finally:
            os.close(lock)

    async with unix_serve(handle, str(socket_path), max_size=32*1024*1024,
                           compression=None, origins=[None]) as listener:
        socket_path.chmod(0o600)
        # Explicit cwd preserves normal local CLI working-directory semantics.
        cwd_args = [] if any(a in ('-C', '--cd') or a.startswith('--cd=') or
                            (a.startswith('-C') and len(a)>2) for a in args) else ['--cd', os.getcwd()]
        cli = await asyncio.create_subprocess_exec(real, '--remote', 'unix://' + str(socket_path),
                *cwd_args, *args, env=env)
        client_ready.set()
        loop = asyncio.get_running_loop()
        # The foreground terminal delivers SIGINT to the real TUI as usual. Keep
        # the bridge alive so Ctrl-C can cancel a turn instead of losing auth.
        installed_signals = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, (lambda: None) if sig == signal.SIGINT else
                    (lambda: cli.terminate() if cli.returncode is None else None))
                installed_signals.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            return await cli.wait()
        finally:
            for sig in installed_signals:
                loop.remove_signal_handler(sig)
            if cli.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    cli.terminate()
                await cli.wait()
            listener.close()
            await listener.wait_closed()
            log_path = status_path.parent / BRIDGE_LOG_NAME
            if bridge_failed:
                reason = getattr(active_bridge, 'failure', None) or 'unknown'
                print(f'xswap auto: CLI bridge disconnected ({reason}); see {log_path} or xswap auto-status.', file=sys.stderr)
            elif getattr(active_bridge, 'last_failure', None):
                # A session that recovered on its own still ended with something wrong in it;
                # on 2026-09-10 nothing pointed the operator at the record afterwards.
                failure = active_bridge.last_failure
                print(f'xswap auto: last failure in this session: {failure["event"]} ({failure["reason"]}); '
                      f'see {log_path}.', file=sys.stderr)
            command = reconnect_command(pool, env, active_bridge)
            if command:
                print('xswap: the temporary connection above is closed. '
                      'Resume this conversation with a new bridge:', file=sys.stderr)
                print(command, file=sys.stderr)


def reconnect_command(pool, env, bridge):
    """Only emit a known conversation and shell-quoted, non-secret metadata."""
    thread = getattr(bridge, 'resume_thread', None)
    manager = getattr(pool, 'manager', None)
    if not thread or not manager or not env.get('CODEX_HOME'):
        return None
    candidates = getattr(bridge, 'resume_candidates', [thread])
    thread = next((t for t in candidates if saved_thread(env['CODEX_HOME'], t)), None)
    if not thread:
        return None
    names = list(dict.fromkeys([bridge.current, *pool.names]))
    return shlex.join(['env', 'CODEX_SWAP_HOME=' + str(manager.root),
        'CODEX_HOME=' + env['CODEX_HOME'], 'xswap', 'run', '--auto',
        '--accounts', ','.join(names), '--', 'resume', thread])


def resume_home(manager, args, default):
    """Explicit UUID resumes use the home that already owns the conversation."""
    command = None
    session_id = None
    iterator = iter(args)
    for arg in iterator:
        if arg in VALUE_FLAGS:
            next(iterator, None)
            continue
        if arg.startswith('-'):
            continue
        if command is None:
            command = arg
            if command not in ('resume', 'fork'):
                return default
        else:
            try:
                session_id = str(uuid.UUID(arg))
            except ValueError:
                return default  # Named sessions and picker/--last keep normal scope.
            break
    if session_id is None:
        return default
    homes = [default, manager.source, Path.home() / '.codex']
    homes.extend(Path(entry['home']) for entry in manager.read()['accounts'].values())
    seen = set()
    matches = []
    for home in homes:
        home = home.resolve()
        if home in seen:
            continue
        seen.add(home)
        for folder in ('sessions', 'archived_sessions'):
            for path in (home / folder).glob(f'**/*-{session_id}.jsonl'):
                if path.is_file() and not path.is_symlink():
                    if home == default.resolve():
                        return default  # Prefer the current runtime if both have copies.
                    matches.append(home)
                    break
    matches = list(dict.fromkeys(matches))
    if len(matches) > 1:
        raise LiveError('This session exists in multiple account homes; resume from its original CODEX_HOME with XSWAP_BYPASS=1.')
    if matches:
        from codex_swap import check_file_store
        check_file_store(matches[0])
        return matches[0]
    return default


def new_run_dir(manager):
    """Create auto/cli-runs/<hex> with every level 0700, repairing an existing `auto`.

    mkdir(parents=True, mode=0o700) applies the mode to the leaf only, so an `auto`
    directory first created by a CLI launch took the umask (0755). switch_running
    then treated the control directory as unsafe and signalled nothing, silently:
    `xswap use` never reached running sessions on such a machine (INT-5085).
    """
    from codex_swap import private_dir
    auto = manager.root / 'auto'
    private_dir(auto)
    private_dir(auto / 'cli-runs')
    # A new session is the natural sweep point: records nobody will read again
    # go before this run's own record exists. scan_runs never reads auto.json,
    # so an unreadable settings file cannot block a launch here.
    scan_runs(manager)
    run_dir = auto / 'cli-runs' / uuid.uuid4().hex
    private_dir(run_dir)
    return run_dir


def launch_cli(manager, accounts, args, dry=False):
    from codex_swap import private_dir
    names = [value.strip() for value in accounts.split(',') if value.strip()]
    if not interactive_args(args):
        raise LiveError('auto CLI supports interactive Codex, resume, fork, and agents; ordinary utility/exec commands use normal authentication')
    real = manager.codex()
    pool = AccountPool(manager, names, real)
    runtime = manager.root / 'auto' / 'cli-codex'
    home = resume_home(manager, args, runtime)
    for value in args:
        try:
            thread_id = str(uuid.UUID(value))
        except (ValueError, TypeError):
            continue
        if 'resume' in args and writer_busy(home, thread_id):
            raise BusyThreadError('This conversation is open in another Codex session. Return to that window or close it before resuming; its writer lock was left intact.')
    if dry:
        print(json.dumps({'mode': 'auto-cli', 'accounts': names, 'CODEX_HOME': str(home),
                          'transport': 'private Unix WebSocket', 'args': args}, indent=2))
        return 0
    if home == runtime:
        private_dir(home.parent)
        private_dir(home)
        _, source = manager.account(names[0])
        for entry in ('config.toml', 'AGENTS.md', 'skills', 'rules'):
            src, dst = source / entry, home / entry
            if src.exists() and not dst.exists() and not dst.is_symlink():
                dst.symlink_to(src, target_is_directory=src.is_dir())
        from xswap_plugins import ensure_plugins
        ensure_plugins(home, source)
        # An in-session Codex update installs under $CODEX_HOME/packages; point that at
        # the reference home so the release never lands inside xswap's state (INT-5186).
        from xswap_relocate import link_packages
        link_packages(manager, home)
    else:
        print('xswap auto: resuming from the original session home', file=sys.stderr)
    run_dir = new_run_dir(manager)
    env = manager.env(home)
    for key in list(env):
        if key.startswith('XSWAP_') or key in ('CODEX_CLI_PATH', 'CODEX_APP_SERVER_WS_URL', 'CODEX_APP_SERVER_USE_LOCAL_DAEMON'):
            env.pop(key, None)
    # macOS Unix socket names have a short length limit. A random 0700 /tmp
    # directory avoids long user paths and prevents access by other users.
    with tempfile.TemporaryDirectory(prefix='xs-', dir='/tmp') as temporary:
        try:
            return asyncio.run(serve_cli(pool, real, args, env, run_dir / 'status.json',
                                         Path(temporary) / 'rpc.sock'))
        finally:
            # A Codex update started from inside this session (ctrl+u) re-points
            # the codex entry; reconnect it before the next plain `codex`.
            with contextlib.suppress(LiveError, OSError):
                reconnect_wrapper(manager)


def read_settings(manager):
    path = manager.root / 'auto.json'
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise LiveError('invalid auto-mode settings; refusing to overwrite') from None


def link_target_path(link, target):
    """Absolute path of a symlink's target, resolving a relative target against the link's directory."""
    return str(link.parent / target) if not Path(target).is_absolute() else target


def swap_symlink(path, target):
    """Atomically point `path` at `target` without a window where the entry is missing."""
    temporary = path.with_name('.xswap-codex-' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


DRIFT_REASONS = ('ok', 'replaced', 'not-wrapped', 'missing', 'not-a-symlink', 'not-user-owned',
    'dangling-target', 'not-executable', 'auto-disabled', 'unreadable')
DRIFT_CAUSES = {
    'replaced': 'a Codex update replaced {path} and xswap could not reconnect it',
    'missing': '{path} does not exist',
    'not-a-symlink': '{path} is not a symlink, so xswap leaves it alone',
    'not-user-owned': '{path} is not owned by this user, so xswap leaves it alone',
    'dangling-target': '{path} -> {target} does not exist, so xswap leaves it alone',
    'not-executable': '{path} -> {target} is not an executable file, so xswap leaves it alone',
    'auto-disabled': 'automatic switching is disabled, so xswap leaves {path} alone (-> {target})',
    'unreadable': '{path} could not be inspected ({error}), so xswap leaves it alone',
}
DRIFT_FIXES = {
    'replaced': 'reconnect it by hand: {reconnect}',
    'missing': 'reinstall Codex or recreate the link, then run: {reconnect}',
    'not-a-symlink': 'replace it with a user-owned symlink to the Codex executable, then run: {reconnect}',
    'not-user-owned': 'make it a user-owned symlink (or use xswap directly), then run: {reconnect}',
    'dangling-target': 'reinstall Codex or point the link at a Codex executable, then run: {reconnect}',
    'not-executable': 'point the link at a Codex executable, then run: {reconnect}',
    'auto-disabled': 'enable automatic switching and connect it: {reconnect}',
    'unreadable': 'check its permissions, then run: {reconnect}',
}


def entry_drift(path, proxy, enabled=True):
    """Classify one `codex` entry against the recorded xswap-codex proxy.

    Codex's standalone updater (ctrl+u in the TUI, `codex upgrade`, the install
    script) re-points the user-owned `codex` symlink at its new release and so
    silently disconnects plain `codex` from xswap. Returns a dict with the same
    keys every time:

      action  'reconnect' (a user-owned symlink now points at another existing
              executable and automatic switching is enabled) or 'skip'
      reason  one of DRIFT_REASONS; 'ok' means the entry runs xswap-codex and
              'replaced' is the only reason paired with 'reconnect'
      path    the entry that was judged
      target  the entry's current link text (may be relative), or None
      real    that target as an absolute path, or None
      proxy   the recorded xswap-codex path, or None
      error   short OS error text for 'unreadable', else None

    Pure: lstat/readlink/access only, never a write. Every condition that used to
    be a silent None is named, so doctor and use/switch can explain a gap nothing
    will ever close on its own. A link-state reason wins over `auto-disabled`:
    those need the same manual repair whether or not switching is on.
    """
    path = Path(path)
    result = {'action': 'skip', 'reason': 'replaced', 'path': str(path),
              'target': None, 'real': None, 'proxy': proxy, 'error': None}
    try:
        # lexists, not exists: a dangling symlink still occupies the entry and
        # takes a different repair than a missing one.
        if not os.path.lexists(path):
            return {**result, 'reason': 'missing'}
        if not path.is_symlink():
            return {**result, 'reason': 'not-a-symlink'}
        target = os.readlink(path)
        result.update(target=target, real=link_target_path(path, target))
        if target == proxy:
            return {**result, 'reason': 'ok'}
        # Ownership is checked after that: an entry already running xswap-codex is
        # connected whoever owns it, which is the rule doctor has always applied.
        if path.lstat().st_uid != os.getuid():
            return {**result, 'reason': 'not-user-owned'}
        real = Path(result['real'])
        if not real.is_file():
            return {**result, 'reason': 'dangling-target'}
        if not os.access(real, os.X_OK):
            return {**result, 'reason': 'not-executable'}
        if real.resolve() == Path(proxy).resolve():
            return {**result, 'reason': 'ok'}
    except (OSError, RuntimeError) as error:
        # RuntimeError: Path.resolve() on a symlink loop before Python 3.13. Its
        # message quotes the loop's own path, so report the kind of failure only;
        # the cause sentence already names the entry.
        return {**result, 'reason': 'unreadable',
                'error': getattr(error, 'strerror', None) or type(error).__name__}
    if not enabled:
        return {**result, 'reason': 'auto-disabled'}
    return {**result, 'action': 'reconnect', 'reason': 'replaced'}


def wrapper_drift(settings):
    """Classify the recorded codex entry; same keys as entry_drift, plus 'not-wrapped'."""
    wrapper = settings.get('wrapper') or {}
    proxy = wrapper.get('proxy')
    if not proxy or not wrapper.get('path'):
        return {'action': 'skip', 'reason': 'not-wrapped', 'path': wrapper.get('path') or None,
                'target': None, 'real': None, 'proxy': proxy or None, 'error': None}
    return entry_drift(wrapper['path'], proxy, bool(settings.get('enabled')))


def describe_drift(drift, reconnect):
    """(cause, fix) for a drift result xswap did not act on; ('', '') for 'ok' and 'not-wrapped'.

    `reconnect` is the `xswap auto-enable --accounts ... --wrap-codex` line for the
    caller's account set. Paths and short OS error text only; never secrets.
    """
    reason = drift.get('reason')
    if reason not in DRIFT_CAUSES:
        return '', ''
    values = {**drift, 'reconnect': reconnect}
    return DRIFT_CAUSES[reason].format(**values), DRIFT_FIXES[reason].format(**values)


def codex_path_entries(settings, env=None, relative=False):
    """Every `codex` a PATH lookup can run, in lookup order, classified against the recorded wrapper.

    Mirrors shutil.which's per-directory test (an existing file with the execute
    bit; a dangling link or a directory is skipped) over the absolute PATH
    directories, so entries[0] is what plain `codex` runs from any working
    directory. A directory listed twice on PATH yields one entry. `kind` is
    'wrapper' when the entry's link target is the recorded proxy or the entry
    resolves to the same file as the proxy, 'foreign' otherwise; `target` is the
    raw link target of a symlink and None for a regular file. `env` supplies PATH
    instead of os.environ, which keeps doctor pure and lets a test pin a PATH
    without patching the process environment.

    `relative` also reports the `codex` a relative PATH element resolves to in the
    current working directory, as kind 'relative'. Nothing may re-point such an
    entry, so every repair path leaves it out (the default); doctor asks for it
    because plain `codex` in that directory still runs it.
    """
    wrapper = settings.get('wrapper') or {}
    proxy = wrapper.get('proxy')
    try:
        proxy_real = os.path.realpath(proxy) if proxy else None
    except OSError:
        proxy_real = None
    entries, seen = [], set()
    for directory in os.get_exec_path(env):
        if not directory:
            continue
        # A relative PATH element (a project's `bin`, a direnv habit) names a different
        # file in every working directory, so it can never be the recorded entry and must
        # never be re-pointed: xswap would rewrite a `codex` shim inside the user's own
        # repository and store a `path` no other directory can find again. Leaving it out
        # of the report as well is what made the bypass read as OK, so it is still listed
        # on request -- classified so no repair can mistake it for something to act on.
        is_relative = not os.path.isabs(directory)
        if is_relative and not relative:
            continue
        candidate = Path(directory) / 'codex'
        # Per PATH directory, not per realpath: on this machine several entries
        # resolve to the same xswap-codex script, and collapsing them would hide
        # both the count doctor reports and the recorded entry's place in the order.
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            target = os.readlink(candidate) if candidate.is_symlink() else None
            real = os.path.realpath(candidate)
        except OSError:
            continue
        kind = 'relative' if is_relative else \
            'wrapper' if proxy and (target == proxy or real == proxy_real) else 'foreign'
        entries.append({'path': str(candidate), 'target': target, 'kind': kind})
    return entries


def wrapper_state(settings, env=None):
    """What plain `codex` runs relative to the recorded wrapper: (state, first, entries).

    2026-09-10: Codex's standalone installer wrote ~/.local/bin/codex ahead of the
    wrapped /opt/homebrew/bin/codex, so plain `codex` ran its own release against
    ~/.codex while every check that read only auto.json reported a connected
    wrapper. `state` is 'unconfigured' (no wrapper record, or automatic switching
    disabled: the record is dormant and the entry was restored), 'absent' (no
    codex on this PATH), 'connected' (the first entry is xswap-codex), 'drifted'
    (the recorded entry itself no longer points at xswap-codex) or 'shadowed'
    (another entry precedes it). `first` is entries[0] or None.
    """
    wrapper = settings.get('wrapper') or {}
    entries = codex_path_entries(settings, env)
    first = entries[0] if entries else None
    if not wrapper.get('path') or not wrapper.get('proxy') or not settings.get('enabled'):
        state = 'unconfigured'
    elif first is None:
        state = 'absent'
    elif first['kind'] == 'wrapper':
        state = 'connected'
    elif first['path'] == wrapper['path']:
        state = 'drifted'
    else:
        state = 'shadowed'
    return state, first, entries


def shadowing_entry(settings, env=None):
    """(path, target) of a wrappable `codex` entry ahead of the recorded one on PATH, or None.

    Acts only when the recorded entry is itself on this PATH: then the new entry
    is what a Codex install put in front of the wrapped one. A PATH without the
    recorded entry (a launchd job, a test fixture) says nothing about the user's
    shell, so nothing is re-pointed from it. wrapper_state already applied the
    `enabled` gate, so entry_drift keeps its default.
    """
    wrapper = settings.get('wrapper') or {}
    state, first, entries = wrapper_state(settings, env)
    if state != 'shadowed' or not any(entry['path'] == wrapper['path'] for entry in entries):
        return None
    drift = entry_drift(first['path'], wrapper['proxy'])
    return (first['path'], drift['target']) if drift['action'] == 'reconnect' else None


def wrapper_records(settings):
    """Every wrapped codex entry with rollback data: the primary `wrapper`, then `wrappers` (0.8.0)."""
    records = []
    for record in [settings.get('wrapper'), *(settings.get('wrappers') or [])]:
        if isinstance(record, dict) and record.get('path') and record.get('proxy') and record.get('originalTarget'):
            records.append(record)
    return records


def set_primary_wrapper(settings, record):
    """Make `record` the primary `wrapper` (the entry plain `codex` runs through).

    A previous primary at another path moves to `wrappers` so auto-disable
    restores it too; an older record for the same path is replaced. One record per
    path. Every wrapper record xswap creates passes through here, so there is one
    place to look for what gets stored.
    """
    kept = {}
    for entry in [settings.get('wrapper'), *(settings.get('wrappers') or [])]:
        if isinstance(entry, dict) and entry.get('path') and entry['path'] != record['path'] and entry['path'] not in kept:
            kept[entry['path']] = entry
    settings['wrapper'] = record
    if kept:
        settings['wrappers'] = list(kept.values())
    else:
        settings.pop('wrappers', None)


def _relink(manager, settings, path, proxy, problem):
    """Persist the rollback record, then atomically point `path` at xswap-codex.

    False after one stderr line when the link cannot be replaced; the record
    already names the release, so the next call retries.
    """
    from codex_swap import atomic_json
    try:
        atomic_json(manager.root / 'auto.json', settings)
        swap_symlink(path, proxy)
    except OSError as error:
        print(f'xswap: {problem} but it could not be reconnected ({error.strerror or error}); '
              f'run: xswap auto-enable --accounts {",".join(settings.get("accounts", []))} --wrap-codex',
              file=sys.stderr)
        return False
    return True


def reconnect_wrapper(manager):
    """Keep plain `codex` connected while automatic switching is enabled.

    Runs on every xswap launch, usage read, and selection, and when a bridged
    session ends (a Codex update usually happens inside one). Two repairs, in
    order: the recorded entry re-pointed by an update is pointed back at
    xswap-codex with the new release recorded as the real Codex (0.7.8); a new
    user-owned `codex` symlink that a Codex install put ahead of the recorded
    entry on PATH is wrapped as well and becomes the primary record, the previous
    one kept in `wrappers` for auto-disable (0.8.0, after the 2026-09-10 bypass).
    Returns the real Codex path after a change, or None when nothing was changed.
    Every skip stays silent here (this runs inside list/usage and so on every
    alert-job tick); wrapper_drift names the reason for doctor and use/switch.
    `xswap auto-disable` restores every record.
    """
    from xswap_relocate import canonical_codex_path, inside_root
    with manager.locked():
        settings = read_settings(manager)
        result = None
        drift = wrapper_drift(settings)
        if drift['action'] == 'reconnect':
            wrapper = settings['wrapper']
            path = Path(drift['path'])
            # install.sh writes "$CODEX_HOME/packages/standalone/current/bin/codex" literally;
            # through an owned home's packages link that spells xswap's root, so record it via
            # the reference home instead. The literal link text stays the rollback target only
            # when nothing had to be rewritten.
            real = canonical_codex_path(manager, drift['real'])
            wrapper.update(originalTarget=drift['target'] if real == drift['real'] else real, realCodex=real)
            misplaced = inside_root(manager, real)
            if not _relink(manager, settings, path, wrapper['proxy'], f'a Codex update replaced {path}'):
                return None
            print(f'xswap: a Codex update had replaced {path}; reconnected it to xswap-codex. Codex is now {real}.'
                  + (' It was installed inside the xswap state directory; run: xswap relocate-codex' if misplaced else ''),
                  file=sys.stderr)
            result = real
        shadow = shadowing_entry(settings)
        if shadow is not None:
            shadow_path, shadow_target = shadow
            previous = settings['wrapper']
            # A shadowing entry is what a Codex install just wrote, so it too can spell a
            # release through an owned home's packages link (Mini, 2026-09-13).
            literal = link_target_path(Path(shadow_path), shadow_target)
            real = canonical_codex_path(manager, literal)
            set_primary_wrapper(settings, {'path': shadow_path, 'originalTarget': shadow_target if real == literal else real,
                                           'realCodex': real, 'proxy': previous['proxy']})
            if not _relink(manager, settings, Path(shadow_path), previous['proxy'],
                           f'{shadow_path} appeared ahead of {previous["path"]} on PATH'):
                return result
            print(f'xswap: {shadow_path} had appeared ahead of {previous["path"]} on PATH and bypassed xswap; '
                  f'connected it to xswap-codex. Codex is now {real}.', file=sys.stderr)
            result = real
    return result


def enable(manager, accounts, wrap=False):
    from codex_swap import atomic_json
    names = [value.strip() for value in accounts.split(',') if value.strip()]
    AccountPool(manager, names, manager.codex())
    settings = read_settings(manager)
    settings.update({'enabled': True, 'accounts': names})
    with manager.locked():
        if wrap:
            proxy = shutil.which('xswap-codex')
            executable = shutil.which('codex')
            if not proxy or not executable:
                raise LiveError('codex and xswap-codex must both be installed')
            # shutil.which joins the raw PATH element, so a relative one yields a relative
            # path: the same entry codex_path_entries refuses to re-point. Absolutising it
            # would wrap whatever `bin/codex` this directory happens to hold (a project's
            # own shim) and record a `path` no other directory can find again, which no
            # later `auto-disable` could restore -- so refuse instead.
            if not os.path.isabs(executable):
                raise LiveError(f'codex was found through a relative PATH entry ({executable}), which names a '
                                'different file in every directory; make that PATH entry absolute, then retry')
            target = Path(executable)
            if target.resolve() != Path(proxy).resolve():
                if not target.is_symlink() or target.lstat().st_uid != os.getuid():
                    raise LiveError('codex wrapper installation requires a user-owned codex symlink; use xswap instead')
                original = os.readlink(target)
                from xswap_relocate import canonical_codex_path, inside_root
                # A release the installer put under an owned home's packages link is recorded
                # through the reference home, so purging auto/ cannot take plain codex with it.
                literal = link_target_path(target, original)
                real = canonical_codex_path(manager, literal)
                set_primary_wrapper(settings, {'path': str(target), 'originalTarget': original if real == literal else real,
                                               'realCodex': real, 'proxy': str(Path(proxy).absolute())})
                if inside_root(manager, real):
                    print(f'xswap: the real Codex ({real}) is inside the xswap state directory; run: xswap relocate-codex',
                          file=sys.stderr)
                # Persist rollback information before the atomic symlink swap.
                atomic_json(manager.root / 'auto.json', settings)
                swap_symlink(target, Path(proxy).absolute())
            elif not settings.get('wrapper'):
                raise LiveError('existing codex wrapper has no recovery information')
        atomic_json(manager.root / 'auto.json', settings)
    print('Auto switching enabled: ' + ' -> '.join(names) + '. New xswap CLI/app sessions use the pool.' +
          (' The codex command is also connected.' if settings.get('wrapper') else ''))


def set_policy(manager, weekly_remaining):
    from codex_swap import atomic_json
    threshold = validate_threshold(weekly_remaining)
    with manager.locked():
        settings = read_settings(manager)
        settings['weeklyRemainingThreshold'] = threshold
        atomic_json(manager.root / 'auto.json', settings)
    print(f'Weekly reserve: {threshold:g}% remaining ({100-threshold:g}% used). '
          'Compatible running bridges apply changes before the next idle turn. '
          'Bridges older than 0.3.2 need a new auto session once; none were stopped.')


def disable(manager):
    from codex_swap import atomic_json
    from xswap_relocate import inside_root
    with manager.locked():
        settings = read_settings(manager)
        for record in wrapper_records(settings):
            path = Path(record['path'])
            if path.is_symlink() and os.readlink(path) == record['proxy']:
                swap_symlink(path, record['originalTarget'])
                # Disconnecting is the user's call even when the release behind the entry is
                # gone or sits inside xswap's state, but plain codex then fails with no hint.
                restored = Path(link_target_path(path, record['originalTarget']))
                if not restored.is_file():
                    print(f'xswap: restored {path} -> {record["originalTarget"]}, but that Codex executable is missing; '
                          'plain codex will not start until Codex is reinstalled (the installer re-creates the link).',
                          file=sys.stderr)
                elif inside_root(manager, str(restored)):
                    print(f'xswap: {path} now points inside the xswap state directory ({restored}); '
                          'run: xswap relocate-codex', file=sys.stderr)
            else:
                # Several entries can be wrapped now, so name the one left behind.
                print(f'codex entry {path} changed outside xswap; left it untouched.', file=sys.stderr)
        # Secondary records are restored (or were changed outside xswap) and have nothing
        # left to recover; only the primary keeps its recovery data, as before.
        settings.pop('wrappers', None)
        settings['enabled'] = False
        atomic_json(manager.root / 'auto.json', settings)
    print('Auto switching disabled for new sessions. Running auto sessions remain active.')


def passthrough_account(manager):
    """The account a pass-through Codex command runs as.

    Same resolution as `xswap run -- exec ...` without --account (Manager.launch_cli
    -> Manager.account(None)): the directory mapping for the cwd, else the account
    selected with `xswap use`. Returns (name, home, mapping_path); mapping_path is
    None when the selection decided. Raises SwapError with a short, non-secret reason
    when no usable account resolves; the caller then keeps the caller's own home.
    """
    from codex_swap import SwapError, check_file_store, identity, is_auth_failed
    name, mapped = manager.resolve_default()
    if not name:
        raise SwapError('no account selected; run: xswap use NAME')
    name, home = manager.account(name)
    if manager.read()['accounts'][name].get('disabled'):
        raise SwapError(f'account {name} is disabled; run: xswap enable {name}')
    check_file_store(home)
    if identity(home) in ('not signed in', 'unreadable auth cache'):
        raise SwapError(f'account {name} is not signed in; run: xswap login {name}')
    if is_auth_failed(manager, name):
        raise SwapError(f'account {name} needs a new login: the usage service rejected it; '
                        f'run: xswap login {name}')
    return name, home, mapped


def passthrough_env(manager, args):
    """Environment for a pass-through Codex command while automatic switching is on.

    `codex exec`, `review`, and the other non-interactive subcommands have no --remote
    hook, so the bridge cannot carry them; before 0.8.0 they ran with the caller's own
    environment, i.e. against ~/.codex and whichever account was signed in there,
    silently ignoring the xswap selection. They now run once as the resolved account
    with the same secret stripping as every other xswap launch (Manager.env). The
    caller's environment is returned unchanged and without a notice for an explicit
    CODEX_HOME, for --help/--version/--remote forms, and for PASSTHROUGH_EXCLUDED
    subcommands; it is returned unchanged with one warning when no usable account
    resolves. Never raises. XSWAP_QUIET=1 silences only the informational line.
    """
    from codex_swap import SwapError
    from xswap_plugins import ensure_plugins
    sub = passthrough_subcommand(args)
    if sub is None or os.environ.get('CODEX_HOME'):
        return dict(os.environ)
    try:
        name, home, mapped = passthrough_account(manager)
        ensure_plugins(home, manager.source)
    except (SwapError, LiveError, OSError) as error:
        reason = str(error) if isinstance(error, (SwapError, LiveError)) else (error.strerror or type(error).__name__)
        print(f'xswap: codex {sub} is running with its own home {manager.source}, not the xswap selection ({reason})',
              file=sys.stderr, flush=True)
        return dict(os.environ)
    if os.environ.get('XSWAP_QUIET') != '1':
        print(f'xswap: running codex {sub} as {name}' + (f' (mapped by {mapped})' if mapped else ''),
              file=sys.stderr, flush=True)
    return manager.env(home)


def missing_codex_message(real, settings):
    """One explicit stderr line for a wrapped codex entry with no real Codex behind it
    (a purged runtime home that held the release, or a lost auto.json record)."""
    names = ','.join(settings.get('accounts') or []) or 'NAME,NAME'
    where = f'{real} is missing' if real else 'auto.json records no realCodex'
    return (f'xswap: the codex command is connected to xswap, but the real Codex executable is unavailable ({where}). '
            f'Run: xswap doctor. Recover: xswap auto-disable, reinstall Codex, then xswap auto-enable --accounts {names} --wrap-codex')


def codex_main():
    from codex_swap import Manager, SwapError
    try:
        manager = Manager()
        settings = read_settings(manager)
        real = (settings.get('wrapper') or {}).get('realCodex')
        if not real or not Path(real).is_file():
            print(missing_codex_message(real, settings), file=sys.stderr)
            return 1
        args = sys.argv[1:]
        bypass = os.environ.get('XSWAP_BYPASS') == '1'
        if settings.get('enabled') and interactive_args(args) and not bypass:
            return launch_cli(manager, ','.join(settings['accounts']), args)
        env = dict(os.environ)
        if settings.get('enabled') and not bypass:
            env = passthrough_env(manager, args)
        os.execve(real, [real, *args], env)
    except BusyThreadError as error:
        print('xswap: ' + str(error), file=sys.stderr)
        return 1
    except (LiveError, SwapError, OSError, ValueError):
        print('xswap: could not start automatic Codex CLI; inspect xswap auto-status or run xswap auto-disable.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


STALE_RUN_SECONDS = 7 * 24 * 3600
STOPPED_RUN_SECONDS = 24 * 3600
MIN_PRUNE_AGE_SECONDS = 60
SESSION_REPORT_LIMIT = 20  # how many records status_data reports; running ones are never dropped
PRUNED_RECORD_KEYS = ('account', 'event', 'bridgeVersion', 'reason')
# The whitelist of bridge status fields a session record may expose. A constant
# because a status.json also holds fields no report may carry (a `stopped`
# record's raw failure payload, anything a newer bridge writes): copying keys by
# name is what keeps `auto-status` free of tokens.
SESSION_KEYS = ('account', 'event', 'switches', 'bridgePid', 'serverPid', 'cliPid', 'updatedAt',
    'bridgeVersion', 'weeklyRemainingThreshold', 'manualSwitchVersion', 'bridgeInstance',
    'manualRequest', 'manualState', 'reason', 'quotaKnown',
    'manualReason', 'candidate', 'lastFailure',
    'conversationId', 'codexHome', 'accounts',
    'verifiedAccount', 'verifiedIdentity', 'verifiedAt', 'verifyReason')


def run_dir_empty(run_dir):
    """True when the record holds nothing but (optionally) its lock file.

    Such a record is invisible in every report: the TUI exited before its
    bridge connected, or the bridge died before its first status write. Any
    other file (a status.json, even an unreadable one; a switch.json; a log)
    keeps the record on the ordinary schedule.
    """
    try:
        return all(entry.name == '.bridge.lock' for entry in run_dir.iterdir())
    except OSError:
        return False


def stopped_cleanly(state):
    """A `stopped` record without a failure reason: the bridge ended the normal way."""
    return isinstance(state, dict) and state.get('event') == 'stopped' and not state.get('reason')


def prune_limit(state, empty):
    """Seconds a non-running record is kept before it is pruned without --prune.

    An empty record has nothing to show. A clean `stopped` record was already
    reported once when the TUI exited; a day keeps it for "what happened
    yesterday". A `stopped` record with a failure reason, or any other last
    event on a record whose lock is free (the bridge ended without its
    `stopped` write: killed, crashed, rebooted), is kept a week so the
    abnormal end stays visible in auto-status.
    """
    if empty:
        return MIN_PRUNE_AGE_SECONDS
    if stopped_cleanly(state):
        return STOPPED_RUN_SECONDS
    return STALE_RUN_SECONDS


def prune_rule(state, empty, age):
    """Short label for the schedule a removal fell under; 'forced' only with --prune."""
    if empty:
        return 'empty'
    if age > STALE_RUN_SECONDS:
        return 'stale'
    if stopped_cleanly(state) and age > STOPPED_RUN_SECONDS:
        return 'stopped'
    return 'forced'


def scan_runs(manager, prune=False, cleanup=True):
    """Read every bridge record; with cleanup, remove the ones nobody will read again.

    Returns (sessions, pruned). A record whose lock is held is never removed,
    nor is anything younger than MIN_PRUNE_AGE_SECONDS. Never reads auto.json:
    a launch sweeps through here, and an unreadable settings file must not stop
    a session from starting.
    """
    run_root = manager.root / 'auto' / 'cli-runs'
    entries = [(manager.root / 'auto' / 'status.json', None)]
    if run_root.is_dir():
        entries += [(run_dir / 'status.json', run_dir) for run_dir in sorted(run_root.iterdir())
                    if run_dir.is_dir() and not run_dir.is_symlink()]
    sessions = []
    pruned = []
    for path, run_dir in entries:
        state = None
        if path.exists():
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                state = None
        lock_path = path.parent / '.bridge.lock'
        try:
            fd = os.open(lock_path, os.O_RDONLY) if lock_path.exists() else None
        except OSError:
            # Sweeps now run from list, the menu bar and every launch, so another
            # one can remove this record between the listing and this open.
            continue
        running = False
        try:
            # Holding the fd (and any lock acquired below) through the removal
            # decision keeps a bridge from starting in the same run dir mid-check.
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    running = True
            if (run_dir is not None and not running and run_dir.parent == run_root and
                    run_dir.is_dir() and not run_dir.is_symlink()):
                updated_at = state.get('updatedAt') if isinstance(state, dict) else None
                if isinstance(updated_at, (int, float)):
                    age = time.time() - updated_at
                else:
                    try:
                        age = time.time() - run_dir.stat().st_mtime
                    except OSError:
                        age = 0
                empty = state is None and run_dir_empty(run_dir)
                # A run dir this fresh may still be between creation and the
                # bridge taking its lock (no lock file, no status.json yet);
                # never race that startup window regardless of --prune.
                if cleanup and age > MIN_PRUNE_AGE_SECONDS and (prune or age > prune_limit(state, empty)):
                    try:
                        shutil.rmtree(run_dir)
                    except OSError:
                        # Lost a race with another prune, or the dir vanished;
                        # never let one bad removal crash the whole report.
                        continue
                    fields = state if isinstance(state, dict) else {}
                    pruned.append({'run': run_dir.name, 'rule': prune_rule(state, empty, age),
                                   'ageSeconds': int(age),
                                   **{key: fields.get(key) for key in PRUNED_RECORD_KEYS}})
                    continue
        finally:
            if fd is not None:
                os.close(fd)
        if state is None:
            continue
        log_path = path.parent / BRIDGE_LOG_NAME
        sessions.append({'surface': 'desktop' if run_dir is None else 'cli', 'running': running,
                         'log': str(log_path) if log_path.is_file() and not log_path.is_symlink() else None,
                         **{key: state.get(key) for key in SESSION_KEYS}})
    return sessions, pruned


def reported_sessions(sessions, limit=SESSION_REPORT_LIMIT):
    """Every running record plus the `limit - running` newest stopped ones, in scan order.

    So the list can exceed `limit` when more than `limit` bridges run: the cap bounds
    `auto-status`, but it must not decide what the version check sees. bridge_hints,
    doctor's `auto cli-runs` row, `upgrade` and the menu bar all read this list, and run
    directories are named by uuid4, so a plain tail silently dropped a running bridge once
    the store held more than `limit` readable records -- the desktop record sorts first and
    went first. On 2026-09-10 three 0.7.2 bridges running next to an installed 0.7.6 is
    exactly the state these hints exist to surface.

    `updatedAt` decides which stopped records go, not scan order: uuid4 names carry no
    time, so dropping by position threw away whichever records happened to sort first --
    the desktop record every time, and with it the recent `lastFailure`/`log` a user is
    looking for while keeping a week-old one.
    """
    if len(sessions) <= limit:
        return list(sessions)
    room = max(0, limit - sum(1 for session in sessions if session.get('running')))
    droppable = [index for index, session in enumerate(sessions) if not session.get('running')]
    newest = sorted(droppable, key=lambda index: sessions[index].get('updatedAt') or 0)
    keep = set(newest[-room:] if room else [])
    return [session for index, session in enumerate(sessions)
            if session.get('running') or index in keep]


def status_data(manager, prune=False, cleanup=True):
    settings = read_settings(manager)
    sessions, pruned = scan_runs(manager, prune=prune, cleanup=cleanup)
    drift = wrapper_drift(settings)
    return {'enabled': settings.get('enabled', False),
                      'accounts': settings.get('accounts', []),
                      'codexWrapped': wrapper_state(settings)[0] == 'connected',
                      'wrapperReason': drift['reason'],
                      'weeklyRemainingThreshold': settings.get('weeklyRemainingThreshold', 0),
                      'pruned': len(pruned), 'prunedRuns': pruned, 'sessions': reported_sessions(sessions)}


SURFACE_LABELS = {'cli': 'CLI', 'desktop': 'Desktop'}


def bridge_label(session):
    """The version a status record reports; every bridge from 0.7.6 on writes it."""
    version = session.get('bridgeVersion')
    return version if isinstance(version, str) and version else 'unknown (older than 0.7.6)'


def reopen_command(session, state, manager):
    """Shell-quoted command that reopens this CLI session's conversation on the
    installed code, or None when no saved conversation can be named.

    Only a validated UUID and non-secret settings reach the string. The same
    saved-conversation rule as reconnect_command: a bootstrap thread that never
    took a turn has no rollout file and must not be advertised.
    """
    try:
        thread = str(uuid.UUID(session.get('conversationId')))
    except (ValueError, TypeError, AttributeError):
        return None
    home = session.get('codexHome') or str(manager.root / 'auto' / 'cli-codex')
    if not saved_thread(home, thread):
        return None
    if state.get('codexWrapped'):
        return shlex.join(['codex', 'resume', thread])
    if state.get('enabled'):
        return shlex.join(['xswap', 'run', '--', 'resume', thread])
    names = [n for n in [session.get('account'), *(session.get('accounts') or [])] if isinstance(n, str) and n]
    names = list(dict.fromkeys(names))
    if len(names) < 2:
        return None
    return shlex.join(['xswap', 'run', '--auto', '--accounts', ','.join(names), '--', 'resume', thread])


def bridge_hint(session, state, manager, target_version):
    """One English line saying how to bring a running session onto target_version."""
    label = bridge_label(session)
    if session.get('surface') == 'desktop':
        return f'bridge {label} · quit and reopen it with xswap app to load {target_version}'
    command = reopen_command(session, state, manager)
    if command:
        return f'bridge {label} · reopen with {command} to load {target_version}'
    return f'bridge {label} · exit and reopen it to load {target_version}'


def bridge_hints(manager, state, target_version):
    """Running sessions whose bridge is not target_version, each with its reopen hint.

    `state` is a status_data() result. A stopped record is never listed: only a live
    bridge keeps running old code. A record without a version predates 0.7.6 and
    counts as outdated. Plain string inequality, so a downgrade (`upgrade --tag` to an
    older release) is flagged too and the text stays true.
    """
    hints = []
    for session in state.get('sessions') or []:
        if not session.get('running') or session.get('bridgeVersion') == target_version:
            continue
        hints.append({'surface': session.get('surface'), 'account': session.get('account'),
                      'bridgeVersion': session.get('bridgeVersion'),
                      'hint': bridge_hint(session, state, manager, target_version)})
    return hints


def describe_bridge_hint(hint):
    """`CLI · ai · bridge 0.7.2 · reopen with ... to load 0.8.0` for doctor and upgrade."""
    return f"{SURFACE_LABELS.get(hint['surface'], 'Unknown')} · {hint['account'] or 'unknown'} · {hint['hint']}"


def show_status(manager, prune=False):
    print(json.dumps(status_data(manager, prune=prune), indent=2))
