"""Connect the unmodified Codex TUI to a private local auto-auth app server."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
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


class WebSocketBridge(Bridge):
    def __init__(self, *args, socket, **kwargs):
        self.socket = socket
        self.outbox = asyncio.Queue(maxsize=4096)
        super().__init__(*args, emit=self.outbox.put_nowait, **kwargs)

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
    connected = False
    client_ready = asyncio.Event()

    async def handle(socket):
        nonlocal connected
        if connected:
            await socket.close(1008, 'one client per CLI session')
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
            print('xswap auto: CLI bridge disconnected; inspect xswap auto-status.', file=sys.stderr)
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


def launch_cli(manager, accounts, args, dry=False):
    from codex_swap import private_dir
    names = [value.strip() for value in accounts.split(',') if value.strip()]
    if not interactive_args(args):
        raise LiveError('auto CLI supports interactive Codex, resume, fork, and agents; ordinary utility/exec commands use normal authentication')
    real = manager.codex()
    pool = AccountPool(manager, names, real)
    home = manager.root / 'auto' / 'cli-codex'
    if dry:
        print(json.dumps({'mode': 'auto-cli', 'accounts': names, 'CODEX_HOME': str(home),
                          'transport': 'private Unix WebSocket', 'args': args}, indent=2))
        return 0
    private_dir(home.parent)
    private_dir(home)
    _, source = manager.account(names[0])
    for entry in ('config.toml', 'AGENTS.md', 'skills', 'rules'):
        src, dst = source / entry, home / entry
        if src.exists() and not dst.exists() and not dst.is_symlink():
            dst.symlink_to(src, target_is_directory=src.is_dir())
    from xswap_plugins import ensure_plugins
    ensure_plugins(home, source)
    run_dir = manager.root / 'auto' / 'cli-runs' / uuid.uuid4().hex
    private_dir(run_dir.parent)
    private_dir(run_dir)
    env = manager.env(home)
    for key in list(env):
        if key.startswith('XSWAP_') or key in ('CODEX_CLI_PATH', 'CODEX_APP_SERVER_WS_URL', 'CODEX_APP_SERVER_USE_LOCAL_DAEMON'):
            env.pop(key, None)
    # macOS Unix socket names have a short length limit. A random 0700 /tmp
    # directory avoids long user paths and prevents access by other users.
    with tempfile.TemporaryDirectory(prefix='xs-', dir='/tmp') as temporary:
        return asyncio.run(serve_cli(pool, real, args, env, run_dir / 'status.json',
                                     Path(temporary) / 'rpc.sock'))


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
                real = str(target.parent / original) if not Path(original).is_absolute() else original
                settings['wrapper'] = {'path': str(target), 'originalTarget': original, 'realCodex': real,
                                       'proxy': str(Path(proxy).absolute())}
                # Persist rollback information before the atomic symlink swap.
                atomic_json(manager.root / 'auto.json', settings)
                temporary = target.with_name('.xswap-codex-' + uuid.uuid4().hex)
                try:
                    temporary.symlink_to(Path(proxy).absolute())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
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
                temporary = path.with_name('.xswap-restore-' + uuid.uuid4().hex)
                try:
                    temporary.symlink_to(wrapper['originalTarget'])
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
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
    except (LiveError, SwapError, OSError, ValueError):
        print('xswap: could not start automatic Codex CLI; inspect xswap auto-status or run xswap auto-disable.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


STALE_RUN_SECONDS = 7 * 24 * 3600
MIN_PRUNE_AGE_SECONDS = 60


def status_data(manager, prune=False, cleanup=True):
    settings = read_settings(manager)
    run_root = manager.root / 'auto' / 'cli-runs'
    entries = [(manager.root / 'auto' / 'status.json', None)]
    if run_root.is_dir():
        entries += [(run_dir / 'status.json', run_dir) for run_dir in sorted(run_root.iterdir())
                    if run_dir.is_dir() and not run_dir.is_symlink()]
    result = []
    pruned = 0
    for path, run_dir in entries:
        state = None
        if path.exists():
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                state = None
        lock_path = path.parent / '.bridge.lock'
        fd = os.open(lock_path, os.O_RDONLY) if lock_path.exists() else None
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
                # A run dir this fresh may still be between creation and the
                # bridge taking its lock (no lock file, no status.json yet);
                # never race that startup window regardless of --prune.
                if cleanup and age > MIN_PRUNE_AGE_SECONDS and (prune or age > STALE_RUN_SECONDS):
                    try:
                        shutil.rmtree(run_dir)
                    except OSError:
                        # Lost a race with another prune, or the dir vanished;
                        # never let one bad removal crash the whole report.
                        continue
                    pruned += 1
                    continue
        finally:
            if fd is not None:
                os.close(fd)
        if state is None:
            continue
        result.append({'surface': 'desktop' if run_dir is None else 'cli',
            'running': running, **{key: state.get(key) for key in
            ('account', 'event', 'switches', 'bridgePid', 'serverPid', 'cliPid', 'updatedAt', 'bridgeVersion', 'weeklyRemainingThreshold')}})
    wrapper = settings.get('wrapper') or {}
    wrapper_path = Path(wrapper.get('path', '/nonexistent-xswap-codex'))
    wrapped = bool(wrapper and wrapper_path.is_symlink() and
                   os.readlink(wrapper_path) == wrapper.get('proxy'))
    return {'enabled': settings.get('enabled', False),
                      'accounts': settings.get('accounts', []), 'codexWrapped': wrapped,
                      'weeklyRemainingThreshold': settings.get('weeklyRemainingThreshold', 0),
                      'pruned': pruned, 'sessions': result[-20:]}


def show_status(manager, prune=False):
    print(json.dumps(status_data(manager, prune=prune), indent=2))
