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

from xswap_live import AccountPool, Bridge, LiveError, validate_threshold


NON_INTERACTIVE = {'exec', 'e', 'review', 'login', 'logout', 'mcp', 'plugin', 'mcp-server',
    'app-server', 'remote-control', 'app', 'completion', 'update', 'doctor', 'sandbox',
    'debug', 'apply', 'queue', 'archive', 'delete', 'migrate-rollouts', 'unarchive',
    'cloud', 'exec-server', 'features', 'help'}
VALUE_FLAGS = {'-c', '--config', '-C', '--cd', '-m', '--model', '-p', '--profile', '-s',
    '--sandbox', '-a', '--ask-for-approval', '--enable', '--disable', '-i', '--image',
    '--add-dir', '--local-provider', '--remote', '--remote-auth-token-env'}


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
            self.resume_thread = str(uuid.UUID(value))
            self.resume_candidates = [self.resume_thread, *[t for t in self.resume_candidates if t != self.resume_thread]][:100]
        except (ValueError, TypeError, AttributeError):
            pass

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
        lock = os.open(status_path.parent / '.bridge.lock', os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
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
            if bridge_failed:
                reason = getattr(active_bridge, 'failure', None) or 'unknown'
                print(f'xswap auto: CLI bridge disconnected ({reason}); inspect xswap auto-status.', file=sys.stderr)
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


def wrapper_drift(settings):
    """The link target a Codex update left behind on the wrapped codex entry, or None.

    Codex's standalone updater (ctrl+u in the TUI, `codex upgrade`, the install
    script) re-points the user-owned `codex` symlink at its new release and so
    silently disconnects plain `codex` from xswap. Only a user-owned symlink whose
    current target is an existing executable other than xswap-codex counts; a
    dangling or foreign link is left for the user.
    """
    wrapper = settings.get('wrapper') or {}
    proxy = wrapper.get('proxy')
    path = Path(wrapper.get('path', ''))
    if not proxy or not wrapper.get('path'):
        return None
    try:
        if not path.is_symlink() or path.lstat().st_uid != os.getuid():
            return None
        target = os.readlink(path)
        if target == proxy:
            return None
        real = Path(link_target_path(path, target))
        if not real.is_file() or not os.access(real, os.X_OK) or real.resolve() == Path(proxy).resolve():
            return None
    except OSError:
        return None
    return target


def reconnect_wrapper(manager):
    """Reconnect the wrapped codex entry after a Codex update replaced it.

    Runs on every xswap launch, usage read, and selection, and when a bridged
    session ends (the update usually happens inside one). Records the updated
    release as the real Codex and points the entry back at xswap-codex. Returns
    the new real path, or None when nothing was changed. Only acts while
    automatic switching is enabled; `xswap auto-disable` restores the entry.
    """
    from codex_swap import atomic_json
    with manager.locked():
        settings = read_settings(manager)
        if not settings.get('enabled'):
            return None
        target = wrapper_drift(settings)
        if target is None:
            return None
        wrapper = settings['wrapper']
        path = Path(wrapper['path'])
        real = link_target_path(path, target)
        wrapper.update(originalTarget=target, realCodex=real)
        try:
            # Persist the new rollback target before the atomic symlink swap.
            atomic_json(manager.root / 'auto.json', settings)
            swap_symlink(path, wrapper['proxy'])
        except OSError as error:
            print(f'xswap: a Codex update replaced {path} but it could not be reconnected ({error.strerror or error}); '
                  f'run: xswap auto-enable --accounts {",".join(settings.get("accounts", []))} --wrap-codex',
                  file=sys.stderr)
            return None
    print(f'xswap: a Codex update had replaced {path}; reconnected it to xswap-codex. '
          f'Codex is now {real}.', file=sys.stderr)
    return real


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
            target = Path(executable)
            if target.resolve() != Path(proxy).resolve():
                if not target.is_symlink() or target.lstat().st_uid != os.getuid():
                    raise LiveError('codex wrapper installation requires a user-owned codex symlink; use xswap instead')
                original = os.readlink(target)
                settings['wrapper'] = {'path': str(target), 'originalTarget': original,
                                       'realCodex': link_target_path(target, original),
                                       'proxy': str(Path(proxy).absolute())}
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
    with manager.locked():
        settings = read_settings(manager)
        wrapper = settings.get('wrapper')
        if wrapper:
            path = Path(wrapper['path'])
            if path.is_symlink() and os.readlink(path) == wrapper['proxy']:
                swap_symlink(path, wrapper['originalTarget'])
            else:
                print('codex entry changed outside xswap; left it untouched.', file=sys.stderr)
        settings['enabled'] = False
        atomic_json(manager.root / 'auto.json', settings)
    print('Auto switching disabled for new sessions. Running auto sessions remain active.')


def codex_main():
    from codex_swap import Manager, SwapError
    try:
        manager = Manager()
        settings = read_settings(manager)
        real = (settings.get('wrapper') or {}).get('realCodex')
        if not real or not Path(real).is_file():
            raise LiveError('original Codex executable is unavailable; inspect auto.json recovery info')
        args = sys.argv[1:]
        if settings.get('enabled') and interactive_args(args) and os.environ.get('XSWAP_BYPASS') != '1':
            return launch_cli(manager, ','.join(settings['accounts']), args)
        os.execve(real, [real, *args], dict(os.environ))
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
PRUNED_RECORD_KEYS = ('account', 'event', 'bridgeVersion', 'reason')
# The whitelist of bridge status fields a session record may expose. A constant
# because a status.json also holds fields no report may carry (a `stopped`
# record's raw failure payload, anything a newer bridge writes): copying keys by
# name is what keeps `auto-status` free of tokens.
SESSION_KEYS = ('account', 'event', 'switches', 'bridgePid', 'serverPid', 'cliPid', 'updatedAt',
    'bridgeVersion', 'weeklyRemainingThreshold', 'manualSwitchVersion', 'bridgeInstance',
    'manualRequest', 'manualState', 'reason', 'quotaKnown')


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
        sessions.append({'surface': 'desktop' if run_dir is None else 'cli', 'running': running,
                         **{key: state.get(key) for key in SESSION_KEYS}})
    return sessions, pruned


def status_data(manager, prune=False, cleanup=True):
    settings = read_settings(manager)
    sessions, pruned = scan_runs(manager, prune=prune, cleanup=cleanup)
    wrapper = settings.get('wrapper') or {}
    wrapper_path = Path(wrapper.get('path', '/nonexistent-xswap-codex'))
    wrapped = bool(wrapper and wrapper_path.is_symlink() and
                   os.readlink(wrapper_path) == wrapper.get('proxy'))
    return {'enabled': settings.get('enabled', False),
                      'accounts': settings.get('accounts', []), 'codexWrapped': wrapped,
                      'weeklyRemainingThreshold': settings.get('weeklyRemainingThreshold', 0),
                      'pruned': len(pruned), 'prunedRuns': pruned, 'sessions': sessions[-20:]}


def show_status(manager, prune=False):
    print(json.dumps(status_data(manager, prune=prune), indent=2))
