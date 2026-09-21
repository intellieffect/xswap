"""Connect the unmodified Codex TUI to a private local auto-auth app server."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import shlex
import shutil  # noqa: F401  patch target: xswap.providers.codex.codex_cli.shutil.rmtree (runs.scan_runs prunes through it)
import signal
import sys
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xswap.core.errors import BusyThreadError
from xswap.core.paths import ROOT_VARIABLE, auto_dir, cli_runs_dir
from xswap.providers.codex.exit_relay import FooterFilter, StdoutRelay, relay_wanted
from xswap.providers.codex.live import BRIDGE_LOG_NAME, AccountPool, Bridge, LiveError

if TYPE_CHECKING:
    from xswap.manager import Manager

# Moved out of this module (INT-5610) and re-exported: `auto.json` reading to
# `settings`, the codex wrapper/symlink machinery to `wrapper`, run bookkeeping
# and the status report to `runs`. The names stay importable from here because
# siblings, the `xswap_cli` shim and the test suite still reach them by this
# path -- and because the suite patches some of them here, which `wrapper._late`
# and `runs._late` rely on.
from xswap.core.settings import read_settings
from xswap.providers.codex.runs import (  # noqa: F401
    MIN_PRUNE_AGE_SECONDS,
    PRUNED_RECORD_KEYS,
    SESSION_KEYS,
    SESSION_REPORT_LIMIT,
    STALE_RUN_SECONDS,
    STOPPED_RUN_SECONDS,
    SURFACE_LABELS,
    bridge_hint,
    bridge_hints,
    bridge_label,
    describe_bridge_hint,
    prune_limit,
    prune_rule,
    reopen_command,
    reported_sessions,
    run_dir_empty,
    scan_runs,
    show_status,
    status_data,
    stopped_cleanly,
)
from xswap.providers.codex.wrapper import (  # noqa: F401
    BYPASS_REASON,
    BYPASS_VARIABLE,
    DEPENDENCY_REASON,
    DRIFT_CAUSES,
    DRIFT_FIXES,
    DRIFT_REASONS,
    GLOBAL_MODULE_PARENTS,
    PROXY_NAME,
    RELATIVE_ENTRY_REASON,
    RELATIVE_RECORD_REASON,
    bypass_set,
    codex_path_entries,
    dependency_entry,
    describe_drift,
    disable,
    enable,
    entry_drift,
    link_target_path,
    path_state,
    reconnect_wrapper,
    recorded_proxies,
    recorded_real_codex,
    set_policy,
    set_primary_wrapper,
    shadowing_entry,
    swap_symlink,
    wrapped_target,
    wrapper_drift,
    wrapper_records,
    wrapper_state,
)

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


def interactive_args(args: list[str]) -> bool:
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


def passthrough_subcommand(args: list[str]) -> str | None:
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


def server_overrides(args: list[str]) -> list[str]:
    result = []
    iterator = iter(args)
    for arg in iterator:
        if arg in ('-c', '--config', '--enable', '--disable'):
            value = next(iterator, None)
            if value is None:
                raise LiveError('missing Codex option value')
            result.extend((arg, value))
        elif arg.startswith(('--config=', '--enable=', '--disable=')) or (arg.startswith('-c') and len(arg) > 2) or arg == '--strict-config':
            result.append(arg)
        elif arg in VALUE_FLAGS:
            next(iterator, None)
    return result


def writer_busy(home: str | Path, thread_id: str) -> bool:
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


def saved_thread(home: str | Path, thread_id: str) -> bool:
    for folder in ('sessions', 'archived_sessions'):
        if any((Path(home) / folder).glob(f'**/*-{thread_id}.jsonl')):
            return True
    return False


class WebSocketBridge(Bridge):
    #: Set from `serve_cli.handle` once the TUI subprocess exists; read by `Bridge.status`.
    client_pid: int | None = None

    def __init__(self, *args: Any, socket: Any, **kwargs: Any) -> None:
        self.socket = socket
        self.outbox: asyncio.Queue[Any] = asyncio.Queue(maxsize=4096)
        self.ready = asyncio.Event()
        self.initialize_result: dict[str, Any] | None = None
        self.resume_thread: str | None = None
        self.thread_requests: set[Any] = set()
        self.list_requests: set[Any] = set()
        self.loaded_threads: set[Any] = set()
        self.resume_candidates: list[str] = []
        super().__init__(*args, emit=self.outbox.put_nowait, **kwargs)
        self.picker_prefix = self.request_prefix + 'picker-'

    def status_log(self, event: str) -> None:
        # The foreground TUI owns the terminal, including stderr. Operational
        # status is still written by Bridge.status for xswap auto-status.
        pass

    async def rpc(self, method: str, params: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
        result = await super().rpc(method, params, timeout)
        if method == 'initialize':
            self.initialize_result = result
        return result

    async def picker_client(self, socket: Any) -> None:
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

    async def on_client(self, message: dict[str, Any]) -> None:
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

    def remember_thread(self, value: str | None) -> None:
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

    def available_threads(self, message: dict[str, Any]) -> dict[str, Any]:
        result = message.get('result')
        if not isinstance(result, dict) or not isinstance(result.get('data'), list):
            return message
        home = self.env.get('CODEX_HOME')
        if not home:
            return message
        rows = [row for row in result['data'] if row.get('id') in self.loaded_threads
                or not writer_busy(home, row.get('id'))]
        return {**message, 'result': {**result, 'data': rows}}

    async def on_server(self, message: dict[str, Any]) -> None:
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

    async def client_reader(self) -> None:
        async def receive() -> None:
            async for text in self.socket:
                await self.on_client(json.loads(text))

        async def write() -> None:
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


async def serve_cli(
    pool: AccountPool,
    real: str,
    args: list[str],
    env: dict[str, str],
    status_path: Path,
    socket_path: Path,
    bridge_class: type[WebSocketBridge] = WebSocketBridge,
    stdout_relay: bool | None = None,
) -> int:
    """One TUI and one child server. No TCP listener and no TUI restart.

    `stdout_relay`: None decides from the real stdout (`relay_wanted`); True/False force it.
    With the relay the TUI's stdout is a PTY copied to the real stdout minus Codex's dead
    `--remote` exit footer (`exit_relay`); stdin/stderr are always the inherited terminal.
    """
    from websockets.asyncio.server import unix_serve
    from websockets.exceptions import ConnectionClosed

    from xswap.manager import SwapError, private_dir
    connected = False
    active_bridge: WebSocketBridge | None = None
    client_ready = asyncio.Event()
    bridge_failed = False

    async def handle(socket: Any) -> None:
        nonlocal connected, active_bridge, bridge_failed
        if connected:
            assert active_bridge is not None  # connected is only set True together with active_bridge  # noqa: S101 -- narrows an invariant the checker can't see across the call; not user input
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
                            (a.startswith('-C') and len(a)>2) for a in args) else ['--cd', str(Path.cwd())]
        relay: StdoutRelay | None = None
        if stdout_relay if stdout_relay is not None else relay_wanted():
            relay = StdoutRelay(FooterFilter(str(socket_path)))
        try:
            # stdout=None inherits, as before; only the relay case hands the TUI a PTY slave.
            cli = await asyncio.create_subprocess_exec(real, '--remote', 'unix://' + str(socket_path),
                    *cwd_args, *args, env=env, stdout=relay.open() if relay else None)
        except BaseException:
            if relay:
                relay.abort()
            raise
        client_ready.set()
        loop = asyncio.get_running_loop()
        if relay:
            relay.start(loop)
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
            if relay:
                # Everything Codex printed (token usage included) reaches the terminal before
                # xswap's own lines.
                relay.close()
            listener.close()
            await listener.wait_closed()
            log_path = status_path.parent / BRIDGE_LOG_NAME
            if bridge_failed:
                reason = getattr(active_bridge, 'failure', None) or 'unknown'
                print(f'xswap auto: CLI bridge disconnected ({reason}); see {log_path} or xswap auto-status.', file=sys.stderr)
            elif active_bridge is not None and active_bridge.last_failure:
                # A session that recovered on its own still ended with something wrong in it;
                # on 2026-09-10 nothing pointed the operator at the record afterwards.
                failure = active_bridge.last_failure
                print(f'xswap auto: last failure in this session: {failure["event"]} ({failure["reason"]}); '
                      f'see {log_path}.', file=sys.stderr)
            for line in exit_notice(cli.returncode, getattr(active_bridge, 'resume_thread', None),
                                    reconnect_command(pool, env, active_bridge)):
                print(line, file=sys.stderr)


def exit_notice(status: int | None, thread: str | None, command: str | None) -> list[str]:
    """The lines xswap adds after the TUI's last output.

    A `--remote` TUI (Codex 0.155) ends by printing "Disconnected from this task. …", a
    `Reconnect: codex --remote unix:///tmp/xs-…/rpc.sock resume ID` line, a `Stop the current
    turn: …` line, and the token usage -- on stdout, with no switch to turn it off. By then the
    socket directory is deleted and the bridge has SIGTERMed its app-server, so the
    Reconnect/Stop lines are dead; `exit_relay` drops that exact block on its way to the
    terminal when stdout is one (and leaves it alone otherwise, or under XSWAP_RAW_EXIT=1). What
    xswap adds is one short line and the one command that works, and nothing for a session
    that never had a conversation, where Codex prints no footer either.
    """
    if not thread:
        return []
    head = 'Session ended' if status == 0 else f'Codex exited with status {status}'
    if not command:
        return [f'xswap: {head}; this conversation was not saved, so there is nothing to resume.']
    return [f'xswap: {head}. Resume this conversation:', command]


def reconnect_command(pool: AccountPool, env: dict[str, str], bridge: Bridge | None) -> str | None:
    """Only emit a known conversation and shell-quoted, non-secret metadata."""
    thread = getattr(bridge, 'resume_thread', None)
    manager = getattr(pool, 'manager', None)
    if not thread or not manager or not env.get('CODEX_HOME'):
        return None
    candidates = getattr(bridge, 'resume_candidates', [thread])
    thread = next((t for t in candidates if saved_thread(env['CODEX_HOME'], t)), None)
    if not thread:
        return None
    assert bridge is not None  # `thread` above is only truthy when getattr(bridge, ...) found one  # noqa: S101 -- narrows an invariant the checker can't see across the call; not user input
    names = list(dict.fromkeys([bridge.current, *pool.names]))
    return shlex.join(['env', 'CODEX_SWAP_HOME=' + str(manager.root),
        'CODEX_HOME=' + env['CODEX_HOME'], 'xswap', 'run', '--auto',
        '--accounts', ','.join(names), '--', 'resume', thread])


def resume_home(manager: Manager, args: list[str], default: Path) -> Path:
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
        from xswap.manager import check_file_store
        check_file_store(matches[0])
        return matches[0]
    return default


def new_run_dir(manager: Manager) -> Path:
    """Create auto/cli-runs/<hex> with every level 0700, repairing an existing `auto`.

    mkdir(parents=True, mode=0o700) applies the mode to the leaf only, so an `auto`
    directory first created by a CLI launch took the umask (0755). switch_running
    then treated the control directory as unsafe and signalled nothing, silently:
    `xswap use` never reached running sessions on such a machine (INT-5085).
    """
    # Through manager, not xswap.core.fsutil: tests patch xswap.manager.private_dir.
    from xswap.manager import private_dir
    auto = auto_dir(manager.root)
    private_dir(auto)
    private_dir(cli_runs_dir(manager.root))
    # A new session is the natural sweep point: records nobody will read again
    # go before this run's own record exists. scan_runs never reads auto.json,
    # so an unreadable settings file cannot block a launch here.
    scan_runs(manager)
    run_dir = cli_runs_dir(manager.root) / uuid.uuid4().hex
    private_dir(run_dir)
    return run_dir


def launch_cli(manager: Manager, accounts: str, args: list[str], dry: bool = False) -> int:
    # Through manager, not xswap.core.fsutil: tests patch xswap.manager.private_dir.
    from xswap.manager import private_dir
    names = [value.strip() for value in accounts.split(',') if value.strip()]
    if not interactive_args(args):
        raise LiveError('auto CLI supports interactive Codex, resume, fork, and agents; ordinary utility/exec commands use normal authentication')
    real = manager.codex()
    pool = AccountPool(manager, names, real)
    runtime = auto_dir(manager.root) / 'cli-codex'
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
                # Concurrent auto launches share this runtime home and neither holds the
                # registry lock here, so the other one can create the same link between the
                # test and the call (70 of 150 probe launches). The link it created is the one
                # this launch wanted, so the loser has nothing to do -- but the FileExistsError
                # reached codex_main, whose OSError branch tells the user to run
                # `xswap auto-disable`: tearing the wrapper down over a race that was won.
                with contextlib.suppress(FileExistsError):
                    dst.symlink_to(src, target_is_directory=src.is_dir())
        from xswap.providers.codex.plugins import ensure_plugins
        ensure_plugins(home, source)
        # An in-session Codex update installs under $CODEX_HOME/packages; point that at
        # the reference home so the release never lands inside xswap's state (INT-5186).
        from xswap.providers.codex.relocate import link_packages
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


def passthrough_account(manager: Manager) -> tuple[str, Path, str | None]:
    """The account a pass-through Codex command runs as.

    Same resolution as `xswap run -- exec ...` without --account (Manager.launch_cli
    -> Manager.account(None)): the directory mapping for the cwd, else the account
    selected with `xswap use`. Returns (name, home, mapping_path); mapping_path is
    None when the selection decided. Raises SwapError with a short, non-secret reason
    when no usable account resolves; the caller then keeps the caller's own home.
    """
    from xswap.manager import SwapError, check_file_store, identity, is_auth_failed
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


def passthrough_env(manager: Manager, args: list[str]) -> dict[str, str]:
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
    from xswap.manager import SwapError
    from xswap.providers.codex.plugins import ensure_plugins
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


def missing_codex_message(real: str | None, settings: dict[str, Any], root: str | Path | None = None) -> str:
    """One explicit stderr line for a wrapped codex entry with no real Codex behind it
    (a purged runtime home that held the release, or a lost auto.json record).

    `root` is the state root this shell read. It belongs in the line because the shell,
    not the machine, chose it: with CODEX_SWAP_HOME exported in one shell and not in
    another, the same `codex` entry answered to a different auto.json -- and in the shell
    without it the recovery printed here cannot work, because `auto-disable` finds no
    record there and `auto-enable --wrap-codex` refuses an entry it has none for.
    """
    names = ','.join(settings.get('accounts') or []) or 'NAME,NAME'
    reconnect = f'xswap auto-enable --accounts {names} --wrap-codex'
    where_root = f' This shell reads xswap state from {root} ({state_root_source()}).' if root else ''
    if real and not Path(real).is_absolute():
        # Reinstalling Codex cannot fix this one: the record, not the installation, is what
        # cannot be resolved, and `auto-disable` leaves the entry it names untouched.
        return (f'xswap: the codex command is connected to xswap, but the real Codex auto.json records ({real}) is '
                f'not an absolute path, so it names a different file in every directory.{where_root} Run: xswap doctor. '
                f'Recover: {DRIFT_FIXES[RELATIVE_RECORD_REASON].format(reconnect=reconnect)}')
    where = f'{real} is missing' if real else 'auto.json records no realCodex'
    return (f'xswap: the codex command is connected to xswap, but the real Codex executable is unavailable '
            f'({where}).{where_root} Run: xswap doctor. Recover: xswap auto-disable, reinstall Codex, then {reconnect}')


def state_root_source(env: dict[str, str] | None = None) -> str:
    """How this environment chose its state root, in the words every surface uses."""
    value = (os.environ if env is None else env).get(ROOT_VARIABLE)
    return f'selected by {ROOT_VARIABLE}' if value else f'the default; {ROOT_VARIABLE} is not set in this shell'


def codex_main() -> int:
    from xswap.manager import Manager, SwapError
    try:
        # create=False: this runs in whatever shell plain `codex` was typed in, and a shell
        # without CODEX_SWAP_HOME reads the default root. Creating it made the failure below
        # leave an empty second state root behind, which the recovery it prints cannot use.
        manager = Manager(create=False)
        settings = read_settings(manager)
        recorded = (settings.get('wrapper') or {}).get('realCodex')
        real = recorded_real_codex(settings)
        if not real or not Path(real).is_file():
            print(missing_codex_message(recorded, settings, manager.root), file=sys.stderr)
            return 1
        args = sys.argv[1:]
        bypass = os.environ.get('XSWAP_BYPASS') == '1'
        if settings.get('enabled') and interactive_args(args) and not bypass:
            return launch_cli(manager, ','.join(settings['accounts']), args)
        env = dict(os.environ)
        if settings.get('enabled') and not bypass:
            env = passthrough_env(manager, args)
        os.execve(real, [real, *args], env)  # noqa: S606 -- argv list, no shell=True; command/args are program-constructed, not user strings
    except BusyThreadError as error:
        print('xswap: ' + str(error), file=sys.stderr)
        return 1
    # KeyError: auto.json is enabled but holds no `accounts`, which `settings['accounts']` above
    # reads. Outside this tuple it left plain `codex` with an unhandled traceback and exit 1 --
    # no line saying which command failed, and none of the recovery the other broken-record
    # branches print -- for a record a half-written `auto-enable` leaves behind.
    except (LiveError, SwapError, OSError, ValueError, KeyError):
        print('xswap: could not start automatic Codex CLI; inspect xswap auto-status or run xswap auto-disable.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
