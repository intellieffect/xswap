"""Direct tests for the wrapped `codex` entry (INT-5186, item 11).

0.7.8's reconnect-after-a-Codex-update fix was exercised only through enable() and
Manager.codex() in test_cli.py; wrapper_drift() had no direct caller in any test and
several of its branches (not user-owned, relative target, a target that resolves to the
proxy through another link, the failed swap, the temp-link cleanup) were untested. These
tests build the wrapper record by hand or through enable() and call the helpers directly,
one branch per test, so a regression names the branch it broke.

The PATH helpers are called with an explicit `env`, which is what that parameter exists
for: doctor and codex_main pass the environment they are about to read or exec with, and
a test pins a PATH without patching the process environment.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import stat
import sys
import time
import unittest
from unittest.mock import ANY, patch

import test_cli
import test_codex_swap
import xswap_doctor as doctor
from codex_swap import Manager, SwapError, __version__, atomic_json
from codex_swap import main as codex_swap_main
from xswap_cli import (RELATIVE_ENTRY_REASON, RELATIVE_RECORD_REASON, STALE_RUN_SECONDS, codex_main,
                       codex_path_entries, disable, enable,
                       launch_cli, link_target_path, read_settings, reconnect_wrapper, shadowing_entry, show_status,
                       status_data, wrapper_drift, wrapper_state)
from xswap_live import LiveError


class WrapperFixture(unittest.TestCase):
    """A registered pool plus the three executables every wrapper test needs.

    wrapped_fixture() (from test_cli) returns (real, updated, proxy, cli): the release
    `codex` pointed at before wrapping, the release a Codex update installs, the
    xswap-codex proxy, and the user-owned `codex` symlink (initially -> real).
    """
    setup_pool = test_cli.CliTests.setup_pool
    wrapped_fixture = test_cli.CliTests.wrapped_fixture
    make_run = test_cli.CliTests.make_run

    def setUp(self):
        test_codex_swap.AccountTests.setUp(self)
        # Every wrapper and status check walks PATH now (INT-5186 item 1); never let a
        # fixture see -- or re-point -- this machine's own codex entries.
        self.enterContext(patch.dict(os.environ, {'PATH': str(self.base)}))

    def which(self, proxy, cli):
        return patch('shutil.which', side_effect=lambda name: str(proxy if name == 'xswap-codex' else cli))

    def connect(self, proxy, cli):
        """`xswap auto-enable --accounts main,second --wrap-codex` against the fixture paths."""
        with self.which(proxy, cli), contextlib.redirect_stdout(io.StringIO()):
            enable(self.manager, 'main,second', wrap=True)

    def wrapper_settings(self, cli, proxy, real, enabled=True):
        """The auto.json record enable() writes, built by hand for the unit tests."""
        return {'enabled': enabled, 'accounts': ['main', 'second'],
                'wrapper': {'path': str(cli), 'originalTarget': str(real),
                            'realCodex': str(real), 'proxy': str(proxy)}}

    def repoint(self, cli, target):
        """What Codex's updater does to the user-owned entry."""
        cli.unlink()
        cli.symlink_to(target)

    def temp_links(self):
        return [p.name for p in self.base.iterdir() if p.name.startswith('.xswap-codex-')]


class WrapperDriftTests(WrapperFixture):
    """wrapper_drift(settings): one classified reason per state of the recorded entry."""

    def test_connected_link_is_ok(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.repoint(cli, proxy)
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['reason'], drift['target']), ('skip', 'ok', str(proxy)))

    def test_link_repointed_to_a_new_executable_is_a_reconnectable_replacement(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.repoint(cli, updated)
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['reason']), ('reconnect', 'replaced'))
        self.assertEqual((drift['target'], drift['real']), (str(updated), str(updated)))

    def test_relative_target_is_returned_verbatim_and_resolved_against_the_link_dir(self):
        # install.sh writes a target relative to the link's directory; the rollback value
        # has to stay the link text, while the release path has to be absolute.
        real, updated, proxy, cli = self.wrapped_fixture()
        relative = str(updated.relative_to(cli.parent))
        self.repoint(cli, relative)
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['target'], drift['real']),
                         ('reconnect', relative, str(updated)))
        self.assertEqual(link_target_path(cli, drift['target']), str(updated))

    def test_dangling_link_is_left_alone(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.repoint(cli, self.base / 'missing')
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['reason'], drift['real']),
                         ('skip', 'dangling-target', str(self.base / 'missing')))

    def test_non_executable_target_is_left_alone(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        notes = self.base / 'notes.txt'
        notes.write_text('fixture')
        notes.chmod(0o600)
        self.repoint(cli, notes)
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['reason'], drift['target']),
                         ('skip', 'not-executable', str(notes)))

    def test_target_that_resolves_to_the_proxy_is_ok(self):
        # Homebrew-style: codex -> alias -> xswap-codex. readlink differs from the recorded
        # proxy string, but plain codex still reaches xswap, so nothing is re-pointed.
        real, updated, proxy, cli = self.wrapped_fixture()
        alias = self.base / 'alias'
        alias.symlink_to(proxy)
        self.repoint(cli, alias)
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['reason'], drift['target']), ('skip', 'ok', str(alias)))

    def test_link_not_owned_by_the_user_is_left_alone(self):
        # The code compares path.lstat().st_uid with os.getuid(); another uid is simulated
        # on the getuid side because a test cannot chown a file to another user. Nothing
        # else inside entry_drift calls getuid, so the patch cannot mask a second check.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.repoint(cli, updated)
        with patch('os.getuid', return_value=os.getuid() + 1):
            drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['reason'], drift['target']),
                         ('skip', 'not-user-owned', str(updated)))

    def test_regular_file_entry_is_left_alone(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        cli.unlink()
        cli.write_text('fixture')
        cli.chmod(0o700)
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['reason'], drift['target']),
                         ('skip', 'not-a-symlink', None))

    def test_missing_entry_is_its_own_reason(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        cli.unlink()
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real))
        self.assertEqual((drift['action'], drift['reason'], drift['target']), ('skip', 'missing', None))

    def test_half_written_wrapper_record_is_not_wrapped(self):
        # A record missing either half names no entry to judge, so there is nothing to
        # report against it; `wrapper: null` is what auto.json holds before --wrap-codex.
        proxy = str(self.base / 'xswap-codex')
        for settings, path, recorded in (({'wrapper': None}, None, None),
                                         ({'wrapper': {'proxy': proxy}}, None, proxy),
                                         ({'wrapper': {'path': str(self.base / 'codex')}},
                                          str(self.base / 'codex'), None)):
            drift = wrapper_drift(settings)
            self.assertEqual((drift['action'], drift['reason'], drift['path'], drift['proxy']),
                             ('skip', 'not-wrapped', path, recorded), settings)

    def test_disabled_automatic_switching_is_a_reason_not_a_silent_skip(self):
        # 0.7.8 returned a bare None here, so doctor could not tell "nothing to do" from
        # "xswap will never fix this by itself" (INT-5186 item 10 moved the gate in here).
        real, updated, proxy, cli = self.wrapped_fixture()
        self.repoint(cli, updated)
        drift = wrapper_drift(self.wrapper_settings(cli, proxy, real, enabled=False))
        self.assertEqual((drift['action'], drift['reason'], drift['target']),
                         ('skip', 'auto-disabled', str(updated)))


class ReconnectWrapperTests(WrapperFixture):
    """reconnect_wrapper(manager): persist the new release, then swap the link atomically."""

    def test_records_the_release_and_swaps_the_link_atomically(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        self.repoint(cli, updated)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = reconnect_wrapper(self.manager)
        self.assertEqual(result, str(updated))
        self.assertEqual(os.readlink(cli), str(proxy))
        wrapper = read_settings(self.manager)['wrapper']
        self.assertEqual(wrapper['originalTarget'], str(updated))
        self.assertEqual(wrapper['realCodex'], str(updated))
        self.assertEqual(wrapper['path'], str(cli))
        self.assertEqual(wrapper['proxy'], str(proxy))
        self.assertEqual(stat.S_IMODE((self.manager.root / 'auto.json').stat().st_mode), 0o600)
        self.assertEqual(self.temp_links(), [])  # swap_symlink leaves no .xswap-codex-* behind
        self.assertIn(f'a Codex update had replaced {cli}; reconnected it to xswap-codex', err.getvalue())
        self.assertIn(f'Codex is now {updated}', err.getvalue())
        self.assertNotIn('fake-token', err.getvalue())

    def test_relative_update_target_records_an_absolute_real_codex(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        relative = str(updated.relative_to(cli.parent))
        self.repoint(cli, relative)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reconnect_wrapper(self.manager), str(updated))
        wrapper = read_settings(self.manager)['wrapper']
        self.assertEqual(wrapper['originalTarget'], relative)  # auto-disable restores the link text
        self.assertEqual(wrapper['realCodex'], str(updated))
        with self.which(proxy, cli):
            self.assertEqual(self.manager.codex(), str(updated))

    def test_second_call_is_quiet_and_writes_nothing(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        self.repoint(cli, updated)
        with contextlib.redirect_stderr(io.StringIO()):
            reconnect_wrapper(self.manager)
        before = (self.manager.root / 'auto.json').read_bytes()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertEqual(err.getvalue(), '')  # this runs on every list/usage, so every alert tick
        self.assertEqual((self.manager.root / 'auto.json').read_bytes(), before)
        self.assertEqual(os.readlink(cli), str(proxy))

    def test_disabled_auto_mode_leaves_drift_alone(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        settings = read_settings(self.manager)
        settings['enabled'] = False
        atomic_json(self.manager.root / 'auto.json', settings)
        self.repoint(cli, updated)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(cli), str(updated))
        self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'], str(real))

    def test_not_user_owned_link_is_left_alone(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        self.repoint(cli, updated)
        with patch('os.getuid', return_value=os.getuid() + 1), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(cli), str(updated))
        self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'], str(real))

    def test_swap_failure_keeps_the_recovery_record_and_names_the_manual_command(self):
        # The rollback target is persisted before the swap; if the swap fails the record
        # still matches what the entry points at, and the user gets the one-line fix.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        self.repoint(cli, updated)
        with patch('xswap_cli.swap_symlink', side_effect=OSError(errno.EACCES, 'Permission denied')), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertIn(f'a Codex update replaced {cli} but it could not be reconnected (Permission denied)',
                      err.getvalue())
        self.assertIn('run: xswap auto-enable --accounts main,second --wrap-codex', err.getvalue())
        self.assertEqual(os.readlink(cli), str(updated))
        wrapper = read_settings(self.manager)['wrapper']
        self.assertEqual(wrapper['realCodex'], str(updated))
        self.assertEqual(wrapper['originalTarget'], str(updated))

    def test_a_failed_swap_leaves_no_temp_link_next_to_the_entry(self):
        # swap_symlink's own failure path: only os.replace failing runs its
        # `finally: temporary.unlink(...)`, and no test reached it -- the neighbouring
        # swap-failure test patches xswap_cli.swap_symlink wholesale, so dropping the
        # cleanup left all tests green while every failed swap (EACCES on the bin
        # directory, a read-only mount) stranded a dangling .xswap-codex-<hex>.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        self.repoint(cli, updated)
        genuine = os.replace

        def refuse_the_temp_link(source, destination, *args, **kwargs):
            if os.path.basename(source).startswith('.xswap-codex-'):
                raise OSError(errno.EACCES, 'Permission denied')
            return genuine(source, destination, *args, **kwargs)

        with patch('xswap_cli.os.replace', side_effect=refuse_the_temp_link), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertEqual(self.temp_links(), [])
        self.assertEqual(os.readlink(cli), str(updated))  # the entry itself is untouched
        self.assertIn('could not be reconnected (Permission denied)', err.getvalue())

    def test_corrupt_settings_raise_without_touching_the_link(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        self.repoint(cli, updated)
        (self.manager.root / 'auto.json').write_text('not json')
        with self.assertRaises(LiveError):
            reconnect_wrapper(self.manager)
        self.assertEqual(os.readlink(cli), str(updated))
        self.assertEqual((self.manager.root / 'auto.json').read_text(), 'not json')

    def test_manager_codex_reports_a_missing_real_binary(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        real.unlink()  # what purging the directory that held the release does
        with self.which(proxy, cli), self.assertRaises(SwapError) as raised:
            self.manager.codex()
        self.assertIn('Original Codex binary is unavailable', str(raised.exception))
        self.assertIn('xswap auto-enable --accounts main,second --wrap-codex', str(raised.exception))


class LaunchExitReconnectTests(WrapperFixture):
    """launch_cli's finally block: the in-session updater (ctrl+u) is undone before the next plain codex."""

    def launch(self, proxy, cli, serve):
        with self.which(proxy, cli), patch('xswap_cli.serve_cli', serve), patch('xswap_plugins.ensure_plugins'), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = launch_cli(self.manager, 'main,second', [])
        return code, err.getvalue()

    def test_reconnects_even_when_the_bridge_fails(self):
        # The existing coverage only reconnects after a clean exit; a bridge that dies is
        # exactly when an update ran, so the repair must not depend on the exit path.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)

        async def failing_serve(pool, real_path, args, env, status_path, socket_path):
            self.repoint(cli, updated)
            raise LiveError('app-server exited')

        with self.which(proxy, cli), patch('xswap_cli.serve_cli', failing_serve), \
                patch('xswap_plugins.ensure_plugins'), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(LiveError):
                launch_cli(self.manager, 'main,second', [])
        self.assertEqual(os.readlink(cli), str(proxy))
        self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'], str(updated))
        self.assertIn('reconnected it to xswap-codex', err.getvalue())

    def test_corrupt_settings_at_exit_do_not_turn_a_clean_exit_into_a_crash(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)

        async def corrupting_serve(pool, real_path, args, env, status_path, socket_path):
            (self.manager.root / 'auto.json').write_text('not json')
            return 0

        code, err = self.launch(proxy, cli, corrupting_serve)
        self.assertEqual(code, 0)
        self.assertEqual(os.readlink(cli), str(proxy))
        self.assertNotIn('Traceback', err)

    def test_exit_status_of_the_tui_is_preserved_through_the_reconnect(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)

        async def serve(pool, real_path, args, env, status_path, socket_path):
            self.repoint(cli, updated)
            return 7

        code, err = self.launch(proxy, cli, serve)
        self.assertEqual(code, 7)
        self.assertEqual(os.readlink(cli), str(proxy))
        self.assertIn('reconnected', err)

    def test_launch_sweeps_stale_records_before_its_own_run_record(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        stale, _ = self.make_run('stale', updated_at=time.time() - STALE_RUN_SECONDS - 3600)

        async def serve(pool, real_path, args, env, status_path, socket_path):
            return 0

        self.assertEqual(self.launch(proxy, cli, serve)[0], 0)
        self.assertFalse(stale.exists())
        runs = [p for p in (self.manager.root / 'auto' / 'cli-runs').iterdir() if p.is_dir()]
        self.assertEqual(len(runs), 1)  # only this launch's own record is left



class PassthroughExitReconnectTests(WrapperFixture):
    """Manager.launch_cli's fixed-account branch: `xswap run -- upgrade` runs Codex's own
    standalone updater as a subprocess, and that updater re-points the wrapped `codex`
    entry. xswap holds the process, so the repair belongs on this exit path too."""

    def run_passthrough(self, proxy, cli, call):
        with patch('codex_swap.Manager', return_value=self.manager), self.which(proxy, cli), \
                patch('subprocess.call', side_effect=call), patch('xswap_plugins.ensure_plugins'), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = codex_swap_main(['run', '--', 'upgrade'])
        return code, err.getvalue()

    def test_upgrade_reconnects_the_entry_its_updater_repointed(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)

        def updater(command, **kwargs):
            self.assertEqual(command[1:], ['upgrade'])
            self.repoint(cli, updated)  # install.sh's update_visible_command
            return 0

        code, err = self.run_passthrough(proxy, cli, updater)
        self.assertEqual(code, 0)
        self.assertEqual(os.readlink(cli), str(proxy))
        self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'], str(updated))
        self.assertIn('reconnected it to xswap-codex', err)
        self.assertEqual(doctor.check_wrapper(read_settings(self.manager), self.manager.read()['accounts'])['status'],
                         doctor.OK)

    def test_exit_status_and_a_corrupt_settings_file_survive_the_reconnect(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)

        def updater(command, **kwargs):
            (self.manager.root / 'auto.json').write_text('not json')
            return 7

        code, err = self.run_passthrough(proxy, cli, updater)
        self.assertEqual(code, 7)
        self.assertNotIn('Traceback', err)


class WrapperEntryTests(WrapperFixture):
    """codex_main: the xswap-codex entry point execs the recorded release for pass-through commands."""

    def entry_env(self):
        return patch.dict(os.environ, {'CODEX_SWAP_HOME': str(self.manager.root), 'CODEX_HOME': str(self.source)})

    def test_utility_command_execs_the_recorded_real_codex(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        with self.entry_env(), patch('sys.argv', ['xswap-codex', '--version']), patch('os.execve') as execve:
            codex_main()
        execve.assert_called_once_with(str(real), [str(real), '--version'], ANY)

    def test_bypass_variable_execs_the_real_codex_for_interactive_args(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        with self.entry_env(), patch.dict(os.environ, {'XSWAP_BYPASS': '1'}), \
                patch('sys.argv', ['xswap-codex', 'resume', '--last']), patch('os.execve') as execve:
            codex_main()
        execve.assert_called_once_with(str(real), [str(real), 'resume', '--last'], ANY)

    def test_missing_real_codex_is_a_short_classified_error(self):
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        real.unlink()
        with self.entry_env(), patch('sys.argv', ['xswap-codex', '--version']), patch('os.execve') as execve, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(codex_main(), 1)
        execve.assert_not_called()
        self.assertIn('the real Codex executable is unavailable', err.getvalue())
        self.assertIn(str(real), err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())


class EnableLockTests(WrapperFixture):
    """enable() reads auto.json inside its own lock, like every other writer of that file."""

    def test_a_commit_made_while_enable_waited_for_the_lock_is_not_discarded(self):
        # enable() read auto.json before taking the lock, so a snapshot taken while a
        # concurrent reconnect_wrapper held it -- one runs on every launch, list, usage read
        # and alert tick, and holds across the OpenClaw subprocess and plugin copies -- was
        # written back on top of that commit. The entry the competitor had just wrapped kept
        # running xswap-codex with no record left: `auto-disable` exits 0 and leaves it, and
        # every surface reads the surviving record and reports OK.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.repoint(cli, proxy)
        record = {'path': str(cli), 'originalTarget': str(real), 'realCodex': str(real), 'proxy': str(proxy)}
        committed = {'enabled': True, 'accounts': ['main', 'second'], 'wrapper': record}
        acquire = self.manager.locked

        @contextlib.contextmanager
        def commit_then_lock():
            # The competitor held the lock for the whole time enable was reading; its commit
            # lands the moment enable is allowed in, which is what makes enable's snapshot stale.
            atomic_json(self.manager.root / 'auto.json', committed)
            with acquire():
                yield

        with patch.object(self.manager, 'codex', lambda: str(real)), \
                patch.object(self.manager, 'locked', commit_then_lock), \
                contextlib.redirect_stdout(io.StringIO()):
            enable(self.manager, 'main,second')
        stored = read_settings(self.manager)
        self.assertEqual(stored['wrapper'], record)  # the wrapped entry still has its rollback record
        self.assertEqual((stored['enabled'], stored['accounts']), (True, ['main', 'second']))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            disable(self.manager)
        self.assertEqual(os.readlink(cli), str(real))  # and auto-disable can restore it


class RecordedProxyTests(WrapperFixture):
    """The recorded `proxy` is a real, absolute xswap-codex -- or there is no record."""

    def test_enable_refuses_an_xswap_codex_found_through_a_relative_path_entry(self):
        # `codex` had the isabs guard; `xswap-codex` did not, and enable absolutised its
        # relative hit against the working directory. From a project whose PATH starts with
        # `bin`, `auto-enable --wrap-codex` recorded <cwd>/bin/xswap-codex as the proxy and
        # pointed the global `codex` at it: plain `codex` in *every* directory then ran a file
        # the project controls, and `_relink` re-applied that path after every Codex update,
        # while doctor read `codex -> xswap-codex` and reported OK.
        real, updated, proxy, cli = self.wrapped_fixture()
        project = self.base / 'project'
        (project / 'bin').mkdir(parents=True)
        rogue = project / 'bin' / 'xswap-codex'
        rogue.write_text('fixture')
        rogue.chmod(0o700)
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)
        with patch('shutil.which', side_effect=lambda name: 'bin/xswap-codex' if name == 'xswap-codex' else str(cli)), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(LiveError) as refused:
            enable(self.manager, 'main,second', wrap=True)
        self.assertIn('xswap-codex was found through a relative PATH entry (bin/xswap-codex)', str(refused.exception))
        self.assertIn('make that PATH entry absolute', str(refused.exception))
        self.assertEqual(os.readlink(cli), str(real))  # the codex entry is untouched
        self.assertNotIn('wrapper', read_settings(self.manager))  # and nothing was recorded

    def test_a_recorded_proxy_that_no_longer_exists_is_its_own_reason(self):
        # xswap re-installed to another prefix (pipx -> uv), or the venv that provided
        # xswap-codex was deleted: the recorded proxy is gone. Nothing stats it, so a Codex
        # update that re-pointed the entry was "reconnected" to the missing file -- plain
        # `codex` stopped resolving at all -- while wrapper_drift still said `ok`, auto-status
        # said wrapped, and doctor blamed the PATH of the directory the entry lives in.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        self.repoint(cli, updated)  # what Codex's updater does
        proxy.unlink()
        settings = read_settings(self.manager)
        self.assertEqual(wrapper_drift(settings)['reason'], 'proxy-missing')
        with patch.dict(os.environ, {'PATH': str(cli.parent)}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
            state = status_data(self.manager, cleanup=False)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(cli), str(updated))  # not relinked to the file that is gone
        self.assertEqual(read_settings(self.manager)['wrapper'], settings['wrapper'])
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (False, 'proxy-missing'))
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': str(cli.parent)})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn('(proxy-missing)', row['detail'])
        self.assertIn(f'the xswap-codex xswap recorded ({proxy}) no longer exists', row['detail'])
        self.assertIn('Fix: reinstall xswap', row['detail'])
        self.assertIn('xswap auto-enable --accounts main,second --wrap-codex', row['detail'])


class SecondXswapCodexTests(WrapperFixture):
    """A second xswap-codex is never recorded as the real Codex (the exec loop)."""

    def second_proxy(self, name='venv'):
        """Another install's xswap-codex: a project venv, or pipx beside uv."""
        directory = self.base / name
        directory.mkdir()
        proxy = directory / 'xswap-codex'
        proxy.write_text('fixture')
        proxy.chmod(0o700)
        return proxy

    def test_enable_keeps_the_record_when_the_entry_already_reaches_another_xswap_codex(self):
        # "Already wrapped?" compared the entry with the xswap-codex `which` found *now*. Re-run
        # from a shell where a second install comes first, `auto-enable --wrap-codex` reported
        # success and rewrote the record to {originalTarget: A/xswap-codex, realCodex:
        # A/xswap-codex, proxy: B/xswap-codex}: the release path was erased from auto.json, doctor
        # named an xswap-codex as the real Codex, `auto-disable` would "restore" codex to
        # xswap-codex, and plain `codex` exec'd xswap-codex, which exec'd itself.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        recorded = read_settings(self.manager)['wrapper']
        other = self.second_proxy()
        with patch('shutil.which', side_effect=lambda name: str(other if name == 'xswap-codex' else cli)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            enable(self.manager, 'main,second', wrap=True)
        settings = read_settings(self.manager)
        self.assertEqual(settings['wrapper'], recorded)  # the release is still what the record names
        self.assertEqual(settings['wrapper']['realCodex'], str(real))
        self.assertNotIn('wrappers', settings)
        self.assertEqual(os.readlink(cli), str(proxy))
        with patch.dict(os.environ, {'PATH': str(cli.parent)}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.manager.codex(), str(real))  # what every launch execs

    def test_a_second_xswap_codex_ahead_on_path_is_not_adopted_as_a_codex_release(self):
        # reconnect_wrapper runs on every launch, list, use and alert tick with no command typed.
        # It classified an entry reaching another install's xswap-codex as foreign, wrapped it,
        # and wrote {realCodex: B/xswap-codex} -- the same exec loop, unattended. Such an entry
        # runs the same entry point against the same auto.json, so it is connected, not foreign.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        recorded = read_settings(self.manager)['wrapper']
        other = self.second_proxy()
        ahead = self.base / 'bin-b'
        ahead.mkdir()
        (ahead / 'codex').symlink_to(other)
        path = os.pathsep.join([str(ahead), str(cli.parent)])
        settings = read_settings(self.manager)
        self.assertEqual([(e['path'], e['kind']) for e in codex_path_entries(settings, {'PATH': path})],
                         [(str(ahead / 'codex'), 'wrapper'), (str(cli), 'wrapper')])
        self.assertIsNone(shadowing_entry(settings, {'PATH': path}))
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
            state = status_data(self.manager, cleanup=False)
            self.assertEqual(self.manager.codex(), str(real))
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(read_settings(self.manager)['wrapper'], recorded)
        self.assertEqual(os.readlink(ahead / 'codex'), str(other))  # the other install is untouched
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (True, 'ok'))
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'OK')
        real_row = [r for r in doctor.check_real_codex(self.manager, settings, self.manager.read()['accounts']) if r['name'] == 'real codex'][0]
        self.assertEqual((real_row['status'], real_row['detail']), ('OK', str(real)))


class DependencyEntryTests(WrapperFixture):
    """A `codex` a project installed for itself is never adopted on xswap's own initiative."""

    def npm_shim(self):
        """npm's shape for a project that depends on @openai/codex."""
        modules = self.base / 'repo' / 'node_modules'
        release = modules / '@openai' / 'codex' / 'bin' / 'codex.js'
        release.parent.mkdir(parents=True)
        release.write_text('fixture')
        release.chmod(0o700)
        (modules / '.bin').mkdir()
        shim = modules / '.bin' / 'codex'
        shim.symlink_to(os.path.relpath(release, modules / '.bin'))
        return shim, release

    def test_a_node_modules_codex_ahead_on_path_is_not_wrapped_and_is_named(self):
        # The shadow repair asked only for a user-owned symlink at an existing executable, so
        # one read-only-looking xswap command in a repository whose node_modules/.bin is on
        # PATH rewrote that link to xswap-codex and made it the primary record. From then on
        # every `xswap run`/`login`/`app`, usage read and pool build exec'd the project's own
        # file with a pool account's CODEX_HOME, and doctor reported OK from any directory.
        from codex_swap import plain_codex_notice
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        recorded = read_settings(self.manager)['wrapper']
        shim, release = self.npm_shim()
        path = os.pathsep.join([str(shim.parent), str(cli.parent)])
        settings = read_settings(self.manager)
        self.assertIsNone(shadowing_entry(settings, {'PATH': path}))
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
            state = status_data(self.manager, cleanup=False)
            notice = plain_codex_notice(self.manager, str(self.base / 'elsewhere'))
            # Nor does xswap exec it itself: the record says where this machine's Codex is.
            self.assertEqual(self.manager.codex(), str(real))
        self.assertEqual(err.getvalue(), '')  # reports only; the repository is untouched
        self.assertEqual(os.readlink(shim), os.path.relpath(release, shim.parent))
        self.assertEqual(read_settings(self.manager)['wrapper'], recorded)
        self.assertNotIn('wrappers', read_settings(self.manager))
        self.assertFalse(state['codexWrapped'])
        # Same one-line shape as every other skip: cannot wrap <entry> (<reason>): <cause>. Fix: <fix>
        self.assertIn(f'xswap cannot wrap {shim} (project-dependency)', notice)
        self.assertIn('belongs to a project (it is inside a node_modules tree)', notice)
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn(f'plain codex runs {shim} -> {os.path.relpath(release, shim.parent)}, not xswap-codex', row['detail'])
        self.assertIn('cannot wrap it (project-dependency)', row['detail'])
        self.assertIn('take that directory off PATH ahead of the codex xswap wrapped', row['detail'])
        self.assertIn('xswap auto-enable --accounts main,second --wrap-codex', row['detail'])

    def test_an_install_outside_a_dependency_tree_is_still_adopted(self):
        # The rule is provenance, not "never adopt": the 2026-09-10 repair -- a Codex install
        # writing a new entry ahead of the wrapped one -- has to keep working.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        ahead = self.base / 'user-bin'
        ahead.mkdir()
        (ahead / 'codex').symlink_to(updated)
        path = os.pathsep.join([str(ahead), str(cli.parent)])
        self.assertEqual(shadowing_entry(read_settings(self.manager), {'PATH': path}),
                         (str(ahead / 'codex'), str(updated)))
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reconnect_wrapper(self.manager), str(updated))
        self.assertEqual(os.readlink(ahead / 'codex'), str(proxy))
        settings = read_settings(self.manager)
        self.assertEqual(settings['wrapper']['path'], str(ahead / 'codex'))
        self.assertEqual([w['path'] for w in settings['wrappers']], [str(cli)])


class RecordedRealCodexTests(WrapperFixture):
    """A `realCodex` that is not absolute is never resolved against the working directory."""

    def legacy_record(self):
        """The 0.7.8 shape: `str(shutil.which('codex'))` verbatim, so both values are relative.

        The project holds that path, which is the state in which every surface reported OK
        and the entry point exec'd the project's file. Returns (project shim, proxy, cli).
        """
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        stored = read_settings(self.manager)
        stored['wrapper'].update(path='bin/codex', originalTarget='bin/codex', realCodex='bin/codex')
        atomic_json(self.manager.root / 'auto.json', stored)
        project = self.base / 'project'
        (project / 'bin').mkdir(parents=True)
        shim = project / 'bin' / 'codex'
        shim.write_text('fixture')
        shim.chmod(0o700)
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)
        return shim, proxy, cli

    def test_codex_main_refuses_a_relative_real_codex_instead_of_execing_it(self):
        # `codex exec hello` inside that repository exec'd the project's own file -- with the
        # pool account's CODEX_HOME -- because the recorded value happened to resolve there,
        # and printed "bin/codex is missing" from every other directory, offering a reinstall
        # that cannot help. The record, not the Codex installation, is what needs repair.
        shim, proxy, cli = self.legacy_record()
        with patch.dict(os.environ, {'CODEX_SWAP_HOME': str(self.manager.root), 'CODEX_HOME': str(self.source)}), \
                patch('sys.argv', ['xswap-codex', 'exec', 'hello']), patch('os.execve') as execve, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(codex_main(), 1)
        execve.assert_not_called()
        self.assertTrue(shim.is_file())  # it was there; that is exactly why it used to run
        self.assertIn('the real Codex auto.json records (bin/codex) is not an absolute path', err.getvalue())
        self.assertIn('point the codex entry that still runs xswap-codex back at the real Codex by hand',
                      err.getvalue())
        self.assertNotIn('reinstall Codex', err.getvalue())

    def test_manager_codex_refuses_it_too_and_doctor_says_so_from_the_directory_that_holds_it(self):
        shim, proxy, cli = self.legacy_record()
        settings = read_settings(self.manager)
        with patch.dict(os.environ, {'PATH': str(cli.parent)}), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SwapError) as refused:
            self.manager.codex()
        self.assertIn('bin/codex is not an absolute path', str(refused.exception))
        row = [r for r in doctor.check_real_codex(self.manager, settings, self.manager.read()['accounts'])
               if r['name'] == 'real codex'][0]
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn('bin/codex is not an absolute path, so it names a different file in every directory',
                      row['detail'])
        self.assertIn('plain codex cannot start', row['detail'])
        self.assertIn('xswap auto-enable --accounts main,second --wrap-codex', row['detail'])
        self.assertTrue(shim.is_file())  # read-only: the project's file is neither run nor touched


class TildePathElementTests(WrapperFixture):
    """A `~` PATH element is expanded: `sh` and `bash` run the `codex` it names."""

    def tilde_fixture(self):
        """An unwrapped `codex` in ~/bin, ahead of the wrapped entry on a literal `~/bin` PATH."""
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        home = self.base / 'home'
        (home / 'bin').mkdir(parents=True)
        standalone = home / 'bin' / 'codex'
        standalone.symlink_to(updated)
        self.enterContext(patch.dict(os.environ, {'HOME': str(home)}))
        return standalone, cli, os.pathsep.join(['~/bin', str(cli.parent)])

    def test_a_tilde_element_is_an_entry_and_a_bypass_is_reported(self):
        # `/bin/sh` and `/bin/bash` both expand a PATH element before the lookup, so
        # PATH="~/bin:..." (a quoted export, a Makefile, a launchd plist) runs ~/bin/codex --
        # the unwrapped standalone, against the caller's own ~/.codex -- while this walk read
        # the literal '~/bin', found nothing there, and every surface was green: doctor OK,
        # auto-status codexWrapped true, and no notice from use/switch.
        standalone, cli, path = self.tilde_fixture()
        settings = read_settings(self.manager)
        self.assertEqual([(e['path'], e['kind']) for e in codex_path_entries(settings, {'PATH': path})],
                         [(str(standalone), 'foreign'), (str(cli), 'wrapper')])
        self.assertEqual(wrapper_state(settings, {'PATH': path})[0], 'shadowed')
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()):
            state = status_data(self.manager, cleanup=False)
        self.assertFalse(state['codexWrapped'])
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn(f'plain codex runs {standalone} -> {self.base}', row['detail'])
        self.assertIn(f'shadows the wrapped entry {cli}', row['detail'])

    def test_an_element_no_home_expands_stays_a_relative_element(self):
        # expanduser returns '~nosuchuser/bin' unchanged; nothing may re-point that either.
        standalone, cli, path = self.tilde_fixture()
        path = os.pathsep.join(['~nosuchuser/bin', str(cli.parent)])
        settings = read_settings(self.manager)
        self.assertEqual([e['path'] for e in codex_path_entries(settings, {'PATH': path})], [str(cli)])


class PathEntriesTests(WrapperFixture):
    """codex_path_entries(settings, env): every codex a PATH lookup can run, in lookup order.

    2026-09-10: the standalone installer's ~/.local/bin/codex preceded the wrapped
    /opt/homebrew/bin/codex, plain codex ran its own release against ~/.codex, and every
    check that read only auto.json reported a connected wrapper.
    """

    def bin_dir(self, name):
        directory = self.base / name
        directory.mkdir()
        return directory

    def executable(self, directory, name='codex'):
        path = directory / name
        path.write_text('fixture')
        path.chmod(0o700)
        return path

    def fixture(self):
        """A connected entry plus the release a Codex install would drop in front of it."""
        real, updated, proxy, cli = self.wrapped_fixture()
        self.repoint(cli, proxy)
        return self.wrapper_settings(cli, proxy, real), cli, proxy, updated

    def test_the_recorded_entry_alone_is_the_wrapper(self):
        settings, cli, proxy, updated = self.fixture()
        self.assertEqual(codex_path_entries(settings, {'PATH': str(cli.parent)}),
                         [{'path': str(cli), 'target': str(proxy), 'kind': 'wrapper'}])

    def test_a_stray_codex_later_on_path_is_foreign(self):
        settings, cli, proxy, updated = self.fixture()
        stray = self.executable(self.bin_dir('stray-bin'))
        entries = codex_path_entries(settings, {'PATH': os.pathsep.join([str(cli.parent), str(stray.parent)])})
        self.assertEqual(entries, [{'path': str(cli), 'target': str(proxy), 'kind': 'wrapper'},
                                   {'path': str(stray), 'target': None, 'kind': 'foreign'}])

    def test_a_stray_codex_earlier_on_path_comes_first(self):
        # entries[0] is what plain `codex` runs, which is the whole point of the order.
        settings, cli, proxy, updated = self.fixture()
        stray = self.executable(self.bin_dir('stray-bin'))
        entries = codex_path_entries(settings, {'PATH': os.pathsep.join([str(stray.parent), str(cli.parent)])})
        self.assertEqual([(e['path'], e['kind']) for e in entries],
                         [(str(stray), 'foreign'), (str(cli), 'wrapper')])

    def test_a_directory_listed_twice_yields_one_entry(self):
        settings, cli, proxy, updated = self.fixture()
        stray = self.executable(self.bin_dir('stray-bin'))
        path = os.pathsep.join([str(cli.parent), str(stray.parent), str(cli.parent)])
        self.assertEqual([e['path'] for e in codex_path_entries(settings, {'PATH': path})],
                         [str(cli), str(stray)])

    def test_non_executable_files_and_missing_directories_are_skipped(self):
        # shutil.which's own test: an entry a PATH lookup would not run is not an entry.
        settings, cli, proxy, updated = self.fixture()
        plain = self.bin_dir('plain-bin') / 'codex'
        plain.write_text('fixture')
        plain.chmod(0o600)
        path = os.pathsep.join([str(cli.parent), str(plain.parent), str(self.base / 'no-such-dir')])
        self.assertEqual([e['path'] for e in codex_path_entries(settings, {'PATH': path})], [str(cli)])

    def test_a_link_that_resolves_to_the_proxy_counts_as_the_wrapper(self):
        # /opt/homebrew/bin/codex -> ~/.local/bin/xswap-codex on the audited machine, and
        # through one more alias here: the link text differs, the file is the same.
        settings, cli, proxy, updated = self.fixture()
        alias = self.base / 'alias'
        alias.symlink_to(proxy)
        brew = self.bin_dir('brew-bin') / 'codex'
        brew.symlink_to(alias)
        entries = codex_path_entries(settings, {'PATH': os.pathsep.join([str(cli.parent), str(brew.parent)])})
        self.assertEqual([(e['path'], e['target'], e['kind']) for e in entries],
                         [(str(cli), str(proxy), 'wrapper'), (str(brew), str(alias), 'wrapper')])

    def test_without_a_wrapper_record_every_entry_is_foreign(self):
        stray = self.executable(self.bin_dir('stray-bin'))
        self.assertEqual(codex_path_entries({}, {'PATH': str(stray.parent)}),
                         [{'path': str(stray), 'target': None, 'kind': 'foreign'}])

    def relative_shim(self, element='bin', target=None):
        """A connected entry plus a project whose PATH starts with `element`, holding its own shim.

        `element` is the relative PATH element ('' is POSIX's spelling of the working
        directory) and `target` what the project's shim points at (the release a Codex
        update installs by default). Returns (settings, shim, cli, updated, path). The
        working directory is the project and is restored on cleanup: a relative PATH element
        resolves against the cwd, so a test that left the runner's in place would be
        asserting about whatever that holds.
        """
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        settings = read_settings(self.manager)
        project = self.base / 'project'
        directory = project / element if element else project
        directory.mkdir(parents=True)
        shim = directory / 'codex'
        shim.symlink_to(target or updated)
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)
        return settings, shim, cli, updated, os.pathsep.join([element, str(cli.parent)])

    def test_a_relative_path_directory_is_not_an_entry_and_is_never_rewritten(self):
        # PATH="bin:/opt/homebrew/bin" with a project-local bin/codex shim (npm's
        # @openai/codex installs exactly that shape). The shadow repair used to wrap it:
        # xswap rewrote a link inside the user's repository and recorded `path: bin/codex`,
        # which `auto-disable` could not find again from any other working directory.
        settings, shim, cli, updated, path = self.relative_shim()
        self.assertEqual([e['path'] for e in codex_path_entries(settings, {'PATH': path})], [str(cli)])
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(shim), str(updated))  # the repository is untouched
        stored = read_settings(self.manager)['wrapper']
        self.assertEqual(stored['path'], str(cli))
        self.assertNotIn('wrappers', read_settings(self.manager))
        # Never re-pointed, but never hidden either: `codex` in this directory runs the shim,
        # so the row whose whole job is "what does plain codex run" has to say so. Reporting
        # OK here (an absolute-only entry list) is the 2026-09-10 bypass with a green light.
        self.assertEqual([(e['path'], e['kind']) for e in codex_path_entries(settings, {'PATH': path}, relative=True)],
                         [('bin/codex', 'relative'), (str(cli), 'wrapper')])
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn(f'plain codex runs bin/codex -> {updated} here, not xswap-codex', row['detail'])
        self.assertIn(f'the wrapped entry {cli} stays bypassed', row['detail'])
        self.assertIn('Fix: make that PATH entry absolute.', row['detail'])

    def test_auto_status_is_not_wrapped_while_a_relative_entry_runs_instead(self):
        # doctor FAILed on this PATH while auto-status still read the absolute-only entry
        # list: codexWrapped stayed True and wrapperReason 'ok' (the recorded link is fine)
        # although plain `codex` here runs the project's shim. That is the 2026-09-10 failure
        # class -- a surface reporting the selection as in effect while plain codex runs
        # something else -- in the one release whose point is that nothing bypasses xswap.
        settings, shim, cli, updated, path = self.relative_shim()
        with patch.dict(os.environ, {'PATH': path}):
            state = status_data(self.manager, cleanup=False)
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (False, 'relative-path-entry'))
        self.assertEqual(RELATIVE_ENTRY_REASON, 'relative-path-entry')
        # The recorded entry is untouched and still runs xswap-codex, which is why the reason
        # has to name the PATH element instead of the entry's link state.
        self.assertEqual(wrapper_drift(read_settings(self.manager))['reason'], 'ok')
        # The condition is the *first* entry: one absolute wrapped entry ahead of the shim and
        # plain `codex` goes through xswap again, from this same directory.
        with patch.dict(os.environ, {'PATH': os.pathsep.join([str(cli.parent), 'bin'])}):
            state = status_data(self.manager, cleanup=False)
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (True, 'ok'))
        # The documented asymmetry survives: with automatic switching off nothing claims plain
        # `codex` goes through xswap, so the reason stays the recorded entry's own.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            disable(self.manager)
        with patch.dict(os.environ, {'PATH': path}):
            state = status_data(self.manager, cleanup=False)
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (False, 'auto-disabled'))

    def test_use_notice_names_the_relative_entry_xswap_cannot_wrap(self):
        # `xswap use` printed nothing on this PATH: wrapper_state read the absolute-only list,
        # called the wrapper connected, and the notice returned None -- so the selection looked
        # like it applied to plain `codex` too, which here runs the project's shim.
        from codex_swap import plain_codex_notice
        settings, shim, cli, updated, path = self.relative_shim()
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            notice = plain_codex_notice(self.manager, str(self.base / 'elsewhere'))
        self.assertEqual(err.getvalue(), '')  # the notice reports; it repairs nothing
        self.assertEqual(len(notice.splitlines()), 1)
        self.assertIn(f'plain `codex` runs bin/codex -> {updated}, not xswap-codex', notice)
        self.assertIn(f'it keeps using {self.manager.source}', notice)
        # Same shape as every other skip reason: cannot wrap <entry> (<reason>): <cause>. Fix: <fix>
        self.assertIn('xswap cannot wrap bin/codex (relative-path-entry): bin/codex is found through a relative '
                      'PATH entry, which the shell resolves against the working directory, so it names a different '
                      'file in every directory and xswap never re-points it. Fix: make that PATH entry absolute; '
                      'until then what plain codex runs depends on the directory you run it from', notice)
        self.assertNotIn('auto-enable', notice)  # no command can wrap this entry
        self.assertEqual(os.readlink(shim), str(updated))  # the repository is untouched
        self.assertEqual(read_settings(self.manager)['wrapper']['path'], str(cli))

    def test_no_repair_path_sees_the_relative_entry_the_reports_now_show(self):
        # The reports ask codex_path_entries for it; every path that re-points a link must keep
        # the default and stay blind to it, or xswap rewrites a shim inside the user's own
        # repository and records a `path` no other directory can resolve (88fe6a1).
        settings, shim, cli, updated, path = self.relative_shim()
        self.assertEqual(wrapper_state(settings, {'PATH': path})[0], 'connected')  # repair view
        self.assertEqual(wrapper_state(settings, {'PATH': path}, relative=True)[0], 'relative')  # report view
        self.assertIsNone(shadowing_entry(settings, {'PATH': path}))
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
            # enable() has its own shutil.which, which returns the relative path here.
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(LiveError) as refused:
                enable(self.manager, 'main,second', wrap=True)
        self.assertIn('relative PATH entry (bin/codex)', str(refused.exception))
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(shim), str(updated))  # the repository is untouched
        self.assertEqual(read_settings(self.manager)['wrapper']['path'], str(cli))
        self.assertNotIn('wrappers', read_settings(self.manager))

    def test_xswap_itself_never_runs_a_codex_found_through_a_relative_path_entry(self):
        # Manager.codex() resolves the binary every launch execs: `xswap login`, `xswap run`, the
        # bridged session, read_limits, and enable's own pool. shutil.which joins the raw PATH
        # element, so here it returns 'bin/codex' and that was returned verbatim -- so
        # `cd repo && xswap login work` execed the repository's own shim with CODEX_HOME set to
        # the pool account's home, handing a project-controlled file that account's auth.json,
        # while the recorded realCodex (and this commit's own doctor row, which says xswap never
        # re-points that entry) was ignored. Same guard as enable: fall back to the record.
        settings, shim, cli, updated, path = self.relative_shim()
        recorded = read_settings(self.manager)['wrapper']['realCodex']
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.manager.codex(), recorded)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(shim), str(updated))  # the repository is untouched
        # Nothing recorded to fall back to: refuse rather than exec whatever this directory holds.
        stored = read_settings(self.manager)
        stored.pop('wrapper')
        atomic_json(self.manager.root / 'auto.json', stored)
        with patch.dict(os.environ, {'PATH': path}), self.assertRaises(SwapError) as refused:
            self.manager.codex()
        self.assertIn('relative PATH entry (bin/codex)', str(refused.exception))
        self.assertIn('make that PATH entry absolute', str(refused.exception))
        self.assertEqual(os.readlink(shim), str(updated))

    def test_the_empty_path_element_is_the_working_directory_and_is_reported(self):
        # `export PATH="$UNSET_VAR:$PATH"` leaves an empty element, which POSIX reads as the
        # working directory: bash and zsh both run ./codex for it and shutil.which returns
        # 'codex'. It was dropped before the relative branch, so doctor said OK, auto-status
        # said wrapped, and use/switch printed nothing -- the bypass this commit exists to
        # surface, in its most common spelling.
        from codex_swap import plain_codex_notice
        settings, shim, cli, updated, path = self.relative_shim(element='')
        self.assertEqual(path, os.pathsep.join(['', str(cli.parent)]))
        self.assertEqual([(e['path'], e['kind']) for e in codex_path_entries(settings, {'PATH': path}, relative=True)],
                         [('./codex', 'relative'), (str(cli), 'wrapper')])
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn(f'plain codex runs ./codex -> {updated} here, not xswap-codex', row['detail'])
        self.assertIn('Fix: make that PATH entry absolute.', row['detail'])
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            state = status_data(self.manager, cleanup=False)
            notice = plain_codex_notice(self.manager, str(self.base / 'elsewhere'))
            self.assertEqual(self.manager.codex(), read_settings(self.manager)['wrapper']['realCodex'])
        self.assertEqual(err.getvalue(), '')  # the reports report; they repair nothing
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (False, RELATIVE_ENTRY_REASON))
        self.assertIn('xswap cannot wrap ./codex (relative-path-entry)', notice)
        # Still no repair view: the empty element is as unrewritable as any other relative one.
        self.assertEqual([e['path'] for e in codex_path_entries(settings, {'PATH': path})], [str(cli)])
        self.assertEqual(wrapper_state(settings, {'PATH': path})[0], 'connected')
        self.assertIsNone(shadowing_entry(settings, {'PATH': path}))
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(shim), str(updated))  # the working directory is untouched
        self.assertEqual(read_settings(self.manager)['wrapper']['path'], str(cli))

    def test_a_relative_xswap_codex_never_hides_a_foreign_entry_that_bypasses_every_directory(self):
        # A foreign *regular file* `codex` ahead of the wrapped entry is the permanent bypass:
        # xswap never overwrites one, so plain `codex` runs it in every directory, for good. The
        # relative caveat was returned before every row that reads the absolute entries, so as
        # long as the working directory happened to hold an xswap-codex, `xswap doctor` said WARN
        # and exited 0, auto-status said codexWrapped, and `xswap use` printed nothing -- the
        # 2026-09-10 bypass with a green gate (2026-09-13).
        from codex_swap import plain_codex_notice
        settings, shim, cli, updated, path = self.relative_shim()
        self.repoint(shim, cli)  # the relative element reaches xswap-codex from this directory
        foreign = self.executable(self.bin_dir('foreign-bin'))
        path = os.pathsep.join(['bin', str(foreign.parent), str(cli.parent)])
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn(f'plain codex runs {foreign}, not xswap-codex, and xswap cannot wrap it (not-a-symlink)',
                      row['detail'])
        self.assertIn(f'The wrapped entry {cli} is shadowed.', row['detail'])
        # The caveat survives as a caveat: it says what this one directory changes, and nothing more.
        self.assertIn(f'In this directory plain codex runs bin/codex -> {cli} first, which is xswap-codex',
                      row['detail'])
        self.assertIn('Fix: make that PATH entry absolute.', row['detail'])
        self.assertEqual(wrapper_state(settings, {'PATH': path}, relative=True)[0], 'shadowed')
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            state = status_data(self.manager, cleanup=False)
            notice = plain_codex_notice(self.manager, str(self.base / 'elsewhere'))
        self.assertEqual(err.getvalue(), '')  # the reports report; they repair nothing
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (False, 'ok'))
        self.assertIn(f'plain `codex` runs {foreign}, not xswap-codex', notice)
        self.assertIn('(not-a-symlink)', notice)
        # Unchanged: no repair path may see, or re-point, the relative element.
        self.assertEqual([e['path'] for e in codex_path_entries(settings, {'PATH': path})],
                         [str(foreign), str(cli)])
        self.assertEqual(os.readlink(shim), str(cli))
        self.assertEqual(read_settings(self.manager)['wrapper']['path'], str(cli))

    def test_a_relative_xswap_codex_never_hides_a_drifted_recorded_entry(self):
        # Same collapse, the other absolute verdict: a Codex update had re-pointed the wrapped
        # entry, and the row that names the drift and the reconnect command never appeared.
        settings, shim, cli, updated, path = self.relative_shim()
        proxy = read_settings(self.manager)['wrapper']['proxy']
        self.repoint(shim, proxy)   # the element reaches xswap-codex directly
        self.repoint(cli, updated)  # what Codex's standalone updater does to the wrapped entry
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn('codex entry changed outside xswap (a Codex update replaces the link)', row['detail'])
        self.assertIn('Reconnect it now: xswap auto-enable --accounts main,second --wrap-codex', row['detail'])
        self.assertIn(f'In this directory plain codex runs bin/codex -> {proxy} first, which is xswap-codex',
                      row['detail'])
        with patch.dict(os.environ, {'PATH': path}):
            state = status_data(self.manager, cleanup=False)
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (False, 'replaced'))

    def test_the_empty_path_element_cannot_bless_a_permanently_bypassed_machine(self):
        # The empty element (`export PATH="$UNSET_VAR:$PATH"`) is POSIX's working directory, so
        # it is the shape this reaches most machines in: ./codex is xswap-codex in the one
        # directory the user happens to be in, and a foreign regular file runs everywhere else.
        settings, shim, cli, updated, path = self.relative_shim(element='')
        self.repoint(shim, cli)
        foreign = self.executable(self.bin_dir('foreign-bin'))
        path = os.pathsep.join(['', str(foreign.parent), str(cli.parent)])
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn(f'plain codex runs {foreign}, not xswap-codex', row['detail'])
        self.assertIn(f'In this directory plain codex runs ./codex -> {cli} first, which is xswap-codex',
                      row['detail'])
        with patch.dict(os.environ, {'PATH': path}):
            state = status_data(self.manager, cleanup=False)
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (False, 'ok'))

    def test_a_relative_entry_that_reaches_xswap_codex_is_not_reported_as_a_bypass(self):
        # The project's bin/codex points at the wrapped entry, so plain `codex` here really does
        # run xswap-codex and the selection is in effect. Classifying the element ahead of the
        # wrapper test made all three surfaces say otherwise: doctor FAILed with "not
        # xswap-codex" (printing the same path as the link's target and as the bypassed entry),
        # auto-status reported codexWrapped false, and the notice claimed plain codex keeps
        # using the source home. A gate wired to doctor's exit code fired on a connected machine.
        from codex_swap import plain_codex_notice
        settings, shim, cli, updated, path = self.relative_shim()
        self.repoint(shim, cli)  # cli exists only after the fixture built it
        self.assertEqual([(e['path'], e['kind']) for e in codex_path_entries(settings, {'PATH': path}, relative=True)],
                         [('bin/codex', 'wrapper'), (str(cli), 'wrapper')])
        self.assertEqual(wrapper_state(settings, {'PATH': path}, relative=True)[0], 'connected')
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': path})
        self.assertEqual(row['status'], 'WARN')  # not a bypass here; still the entry xswap cannot wrap
        self.assertIn(f'plain codex runs bin/codex -> {cli} here, which is xswap-codex', row['detail'])
        self.assertIn(f'whether plain codex reaches the wrapped entry {cli} depends on the directory', row['detail'])
        self.assertIn('Fix: make that PATH entry absolute.', row['detail'])
        self.assertNotIn('not xswap-codex', row['detail'])
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            state = status_data(self.manager, cleanup=False)
            self.assertIsNone(plain_codex_notice(self.manager, str(self.base / 'elsewhere')))
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (True, 'ok'))
        self.assertEqual(err.getvalue(), '')
        # Reported as connected, still never re-pointed: the path stays relative, which is what
        # keeps it out of the repair view and out of the recorded entry's place in the order.
        self.assertEqual([e['path'] for e in codex_path_entries(settings, {'PATH': path})], [str(cli)])
        self.assertIsNone(shadowing_entry(settings, {'PATH': path}))
        with patch.dict(os.environ, {'PATH': path}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(shim), str(cli))
        self.assertEqual(read_settings(self.manager)['wrapper']['path'], str(cli))

    def test_a_recorded_relative_entry_is_never_resolved_against_the_working_directory(self):
        # 0.7.8 stored `path: str(shutil.which('codex'))` with no absolutising, so a shell whose
        # PATH held a relative element recorded `bin/codex`, and 0.8.0 has no migration. Every
        # launch, list, usage, and use runs reconnect_wrapper: from an unrelated repository that
        # ships its own bin/codex it classified the record `replaced`, rewrote *that* repository's
        # shim to xswap-codex, overwrote originalTarget/realCodex with its target, and doctor
        # still read `wrapper OK` from the absolute PATH walk. `auto-disable` then restored the
        # foreign shim and left the entry xswap really wrapped on xswap-codex with no record.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        stored = read_settings(self.manager)
        stored['wrapper']['path'] = 'bin/codex'
        atomic_json(self.manager.root / 'auto.json', stored)
        foreign = self.base / 'foreign-repo'
        (foreign / 'bin').mkdir(parents=True)
        shim = foreign / 'bin' / 'codex'
        shim.symlink_to(updated)
        cwd = os.getcwd()
        os.chdir(foreign)
        self.addCleanup(os.chdir, cwd)
        settings = read_settings(self.manager)
        drift = wrapper_drift(settings)
        self.assertEqual((drift['action'], drift['reason'], drift['path'], drift['target']),
                         ('skip', RELATIVE_RECORD_REASON, 'bin/codex', None))
        self.assertEqual(RELATIVE_RECORD_REASON, 'relative-record')
        with patch.dict(os.environ, {'PATH': str(cli.parent)}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(reconnect_wrapper(self.manager))
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(os.readlink(shim), str(updated))  # the unrelated repository is untouched
        self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'], str(real))
        row = doctor.check_wrapper(settings, self.manager.read()['accounts'], {'PATH': str(cli.parent)})
        self.assertEqual(row['status'], 'FAIL')  # the PATH walk alone reported `wrapper OK`
        self.assertIn('the wrapper record xswap stored is unusable (relative-record)', row['detail'])
        self.assertIn('the recorded codex entry bin/codex is not an absolute path', row['detail'])
        self.assertIn('point the codex entry that still runs xswap-codex back at the real Codex by hand',
                      row['detail'])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            disable(self.manager)
        self.assertIn('codex entry bin/codex is not an absolute path; left it untouched', err.getvalue())
        self.assertEqual(os.readlink(shim), str(updated))  # not "restored" over the repository's shim
        self.assertEqual(os.readlink(cli), str(proxy))

    def test_enable_refuses_a_codex_found_through_a_relative_path_entry(self):
        # `auto-enable --wrap-codex` runs its own shutil.which, which joins the raw PATH
        # element and does not absolutise it: from a project whose PATH starts with `bin`
        # that returns `bin/codex`, and enable wrapped the repository's own shim and stored
        # `path: bin/codex`. From any other directory the record then reads as `missing`,
        # doctor's fix cannot work, and `auto-disable` printed "changed outside xswap; left
        # it untouched" -- leaving the project's shim on xswap-codex for good.
        real, updated, proxy, cli = self.wrapped_fixture()
        project = self.base / 'project'
        (project / 'bin').mkdir(parents=True)
        shim = project / 'bin' / 'codex'
        shim.symlink_to(updated)
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)
        with patch.dict(os.environ, {'PATH': os.pathsep.join(['bin', str(self.base)])}):
            self.assertEqual(shutil.which('codex'), 'bin/codex')  # what enable looks up
            # With no record to fall back to, enable's pool (Manager.codex(), which is what the
            # pool would exec) refuses the same relative result one step earlier; enable's own
            # check still raises LiveError whenever a recorded Codex resolves the pool first
            # (test_no_repair_path_sees_the_relative_entry_the_reports_now_show).
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SwapError) as refused:
                enable(self.manager, 'main,second', wrap=True)
        self.assertIn('relative PATH entry (bin/codex)', str(refused.exception))
        self.assertEqual(os.readlink(shim), str(updated))  # the repository is untouched
        self.assertNotIn('wrapper', read_settings(self.manager))

    def test_doctor_fails_naming_the_entry_that_shadows_the_wrapped_one(self):
        settings, cli, proxy, updated = self.fixture()
        stray = self.bin_dir('stray-bin') / 'codex'
        stray.symlink_to(updated)
        accounts = {'main': {}, 'second': {}}
        row = doctor.check_wrapper(settings, accounts,
                                  env={'PATH': os.pathsep.join([str(stray.parent), str(cli.parent)])})
        self.assertEqual(row['status'], 'FAIL')
        self.assertIn(f'plain codex runs {stray} -> {updated}, not xswap-codex', row['detail'])
        self.assertIn(f'shadows the wrapped entry {cli}', row['detail'])
        self.assertIn('xswap auto-enable --accounts main,second --wrap-codex', row['detail'])
        ok = doctor.check_wrapper(settings, accounts, env={'PATH': str(cli.parent)})
        self.assertEqual((ok['status'], ok['detail']), ('OK', f'{cli} -> xswap-codex'))
        self.assertEqual(os.readlink(cli), str(proxy))  # doctor is read-only


class StateRootTests(WrapperFixture):
    """CODEX_SWAP_HOME decides which auto.json a shell's `codex` obeys; say so where it decides."""

    def fake_home(self):
        """A HOME with no xswap state, so the default root is this test's to inspect."""
        home = self.base / 'fake-home'
        home.mkdir()
        return home, home / '.local/share/codex-swap'

    def test_a_pass_through_that_finds_no_state_names_the_root_and_creates_nothing(self):
        # The xswap-codex entry point runs in whatever shell plain `codex` was typed in. Without
        # CODEX_SWAP_HOME it read the default root, reported the real Codex as unavailable
        # without naming any root -- and created an empty ~/.local/share/codex-swap on the way,
        # a second, stateless root in which neither repair it printed can run (2026-09-13).
        home, default = self.fake_home()
        with patch.dict(os.environ, {'HOME': str(home), 'CODEX_HOME': str(self.source)}), \
                patch('sys.argv', ['xswap-codex', '--version']), patch('os.execve') as execve, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            os.environ.pop('CODEX_SWAP_HOME', None)
            self.assertEqual(codex_main(), 1)
        execve.assert_not_called()
        self.assertFalse(default.exists())  # a failing pass-through invents no state root
        self.assertIn('the real Codex executable is unavailable (auto.json records no realCodex)', err.getvalue())
        self.assertIn(f'This shell reads xswap state from {default} '
                      '(the default; CODEX_SWAP_HOME is not set in this shell)', err.getvalue())

    def test_a_pass_through_names_the_root_a_variable_chose(self):
        # The same failure in the shell that does export it: the root is the fact that tells the
        # two shells apart, so it is in the line whichever of them printed it.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        real.unlink()
        with patch.dict(os.environ, {'CODEX_SWAP_HOME': str(self.manager.root), 'CODEX_HOME': str(self.source)}), \
                patch('sys.argv', ['xswap-codex', '--version']), patch('os.execve') as execve, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(codex_main(), 1)
        execve.assert_not_called()
        self.assertIn(f'This shell reads xswap state from {self.manager.root} (selected by CODEX_SWAP_HOME)',
                      err.getvalue())

    def test_connecting_the_codex_command_says_which_root_the_record_went_to(self):
        # `auto-enable --wrap-codex` writes the record that decides what plain `codex` does, into
        # the root this one shell named, and said nothing about it: every other shell, launchd
        # job and desktop app reads its own root and runs a different selection there.
        real, updated, proxy, cli = self.wrapped_fixture()
        home, default = self.fake_home()
        with patch.dict(os.environ, {'HOME': str(home), 'CODEX_SWAP_HOME': str(self.manager.root)}), \
                self.which(proxy, cli), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            enable(self.manager, 'main,second', wrap=True)
        self.assertIn('The codex command is also connected.', out.getvalue())
        self.assertIn(f"this shell's CODEX_SWAP_HOME put that record in {self.manager.root}", err.getvalue())
        self.assertIn(f'without CODEX_SWAP_HOME reads {default} instead', err.getvalue())
        self.assertIn(f'Export CODEX_SWAP_HOME={self.manager.root} wherever codex runs', err.getvalue())
        self.assertFalse(default.exists())  # naming the other root never creates it

    def test_doctor_names_the_root_only_when_a_variable_decided_it(self):
        home, default = self.fake_home()
        with patch.dict(os.environ, {'HOME': str(home)}):
            os.environ.pop('CODEX_SWAP_HOME', None)
            self.assertIsNone(doctor.check_state_root(self.manager))  # every shell reads the same root
            row = doctor.check_state_root(self.manager, {'CODEX_SWAP_HOME': str(self.manager.root)})
        self.assertEqual(row['status'], 'WARN')
        self.assertIn(f'{self.manager.root}, selected by CODEX_SWAP_HOME', row['detail'])
        self.assertIn(f'without that variable reads {default} instead', row['detail'])
        self.assertIn(f'Fix: export CODEX_SWAP_HOME={self.manager.root} wherever codex runs', row['detail'])
        # A variable that names the default root splits nothing: it is what every shell reads.
        with patch.dict(os.environ, {'HOME': str(home), 'CODEX_SWAP_HOME': str(default)}):
            row = doctor.check_state_root(Manager(root=default, create=False))
        self.assertEqual((row['status'], row['detail']),
                         ('OK', f'{default} (CODEX_SWAP_HOME names the default root)'))
        self.assertFalse(default.exists())


class BypassVariableTests(WrapperFixture):
    """XSWAP_BYPASS=1: every surface says the selection is off, and it stops at the session edge."""

    def test_every_surface_reports_a_shell_whose_codex_passes_through(self):
        # The variable is documented as a one-command prefix, which is what leads to exporting
        # it; exported, every `codex` in that shell runs with the caller's own CODEX_HOME,
        # account and API key. doctor's wrapper row still read `-> xswap-codex` and exited 0,
        # auto-status reported codexWrapped, and `xswap use` printed nothing (2026-09-13).
        from codex_swap import plain_codex_notice
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        settings = read_settings(self.manager)
        path, accounts = str(cli.parent), self.manager.read()['accounts']
        # Without the variable the same fixture is a connected machine with nothing to report.
        self.assertEqual(doctor.check_wrapper(settings, accounts, {'PATH': path})['status'], 'OK')
        self.assertIsNone(doctor.check_bypass(settings, {'PATH': path}))
        with patch.dict(os.environ, {'PATH': path, 'XSWAP_BYPASS': '1'}), contextlib.redirect_stderr(io.StringIO()) as err:
            row = doctor.check_bypass(settings)
            reported = [r for r in doctor.run(self.manager) if r['name'] == 'codex bypass']
            state = status_data(self.manager, cleanup=False)
            notice = plain_codex_notice(self.manager, str(self.base / 'elsewhere'))
        self.assertEqual(err.getvalue(), '')  # the reports report; they repair nothing
        self.assertEqual((row['name'], row['status']), ('codex bypass', 'FAIL'))
        self.assertIn('XSWAP_BYPASS=1 is set here (bypass-variable)', row['detail'])
        self.assertIn("hands every command straight to the real Codex with the caller's own CODEX_HOME, account "
                      'and API keys', row['detail'])
        self.assertIn(f'the wrapped entry {cli} applies no selection in this shell', row['detail'])
        self.assertIn('Fix: unset XSWAP_BYPASS in this shell', row['detail'])
        self.assertEqual([r['status'] for r in reported], ['FAIL'])  # and `xswap doctor` exits 1
        self.assertEqual((state['codexWrapped'], state['wrapperReason']), (False, 'bypass-variable'))
        self.assertIn('plain `codex` does not go through xswap here (bypass-variable)', notice)
        self.assertIn(f'it keeps using {self.manager.source}', notice)
        self.assertIn('Fix: unset XSWAP_BYPASS in this shell', notice)
        self.assertEqual(len(notice.splitlines()), 1)
        # The record itself is untouched: this is the environment's doing, not the entry's.
        self.assertEqual(wrapper_drift(read_settings(self.manager))['reason'], 'ok')

    def test_a_dormant_record_reports_the_variable_as_pending_instead_of_failing(self):
        # With automatic switching off nothing claims plain `codex` goes through xswap, so the
        # variable breaks no promise yet -- it still decides what happens once it is connected.
        row = doctor.check_bypass({}, {'XSWAP_BYPASS': '1'})
        self.assertEqual(row['status'], 'WARN')
        self.assertIn('takes effect as soon as the codex command is connected', row['detail'])
        self.assertIsNone(doctor.check_bypass({}, {}))
        self.assertIsNone(doctor.check_bypass({}, {'XSWAP_BYPASS': '0'}))

    def test_the_variable_does_not_follow_a_launch_into_the_session_it_starts(self):
        # Manager.env copied the caller's environment and kept XSWAP_BYPASS, so a session xswap
        # had just started -- with a pool account's CODEX_HOME already exported -- handed every
        # nested `codex` straight through: a session whose selection nothing could change.
        self.manager.register('main')
        probe = self.base / 'probe-codex'
        # This fixture pins PATH to its own directory, so name the interpreter outright.
        probe.write_text(f'#!{sys.executable}\nimport os\nassert "XSWAP_BYPASS" not in os.environ\n')
        probe.chmod(0o700)
        with patch.dict(os.environ, {'XSWAP_BYPASS': '1'}), \
                patch.object(self.manager, 'codex', return_value=str(probe)):
            self.assertEqual(self.manager.launch_cli(None, [str(self.source)]), 0)
        self.assertNotIn('XSWAP_BYPASS', self.manager.env(self.source))


class RunDirTests(WrapperFixture):
    """auto/cli-runs records next to the reconnect: what status and doctor say about them."""

    def age(self, path, seconds):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def report(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            show_status(self.manager)
        return json.loads(out.getvalue())

    def test_empty_record_is_pruned_by_default(self):
        run_dir = self.manager.root / 'auto' / 'cli-runs' / 'empty-old'
        run_dir.mkdir(parents=True)
        self.age(run_dir, STALE_RUN_SECONDS + 3600)
        report = self.report()
        self.assertFalse(run_dir.exists())
        self.assertEqual(report['pruned'], 1)
        self.assertEqual([(r['run'], r['rule']) for r in report['prunedRuns']], [('empty-old', 'empty')])

    def test_corrupt_record_older_than_a_week_is_pruned_by_default(self):
        run_dir, _ = self.make_run('corrupt-old')
        (run_dir / 'status.json').write_text('not json')
        self.age(run_dir, STALE_RUN_SECONDS + 3600)
        report = self.report()
        self.assertFalse(run_dir.exists())
        self.assertEqual(report['pruned'], 1)
        # An unreadable status is content, so the record keeps the week, not the minute.
        self.assertEqual([(r['run'], r['rule']) for r in report['prunedRuns']], [('corrupt-old', 'stale')])

    def test_fresh_stopped_record_is_reported_not_running_and_kept(self):
        run_dir, _ = self.make_run('stopped-fresh', updated_at=time.time() - 300,
                                   event='stopped', reason='app-server exited')
        report = self.report()
        sessions = [s for s in report['sessions'] if s['event'] == 'stopped']
        self.assertEqual(len(sessions), 1)
        self.assertIs(sessions[0]['running'], False)
        self.assertEqual(sessions[0]['reason'], 'app-server exited')
        self.assertTrue(run_dir.exists())

    def test_doctor_counts_every_record_and_only_held_locks_as_running(self):
        running, fd = self.make_run('running', hold_lock=True)
        self.addCleanup(os.close, fd)
        stopped, _ = self.make_run('stopped')
        # make_run writes only updatedAt/event/reason, and a running bridge whose version
        # is unknown turns this row WARN with a reopen hint, so write the record by hand.
        for run_dir in (running, stopped):
            (run_dir / 'status.json').write_text(json.dumps(
                {'account': 'main', 'updatedAt': time.time(), 'bridgeVersion': __version__}))
        (self.manager.root / 'auto' / 'cli-runs' / 'empty').mkdir()  # invisible in every session report
        row = doctor.check_auto_runs(self.manager)
        self.assertEqual((row['status'], row['detail']), ('OK', '3 run(s), 1 running'))

    def test_auto_session_running_needs_a_held_lock_on_that_account(self):
        stale, _ = self.make_run('stale', updated_at=time.time())
        (stale / 'status.json').write_text(json.dumps({'account': 'main', 'updatedAt': time.time()}))
        self.assertFalse(self.manager._auto_session_running('main'))
        live, fd = self.make_run('live', updated_at=time.time(), hold_lock=True)
        self.addCleanup(os.close, fd)
        (live / 'status.json').write_text(json.dumps({'account': 'main', 'updatedAt': time.time()}))
        self.assertTrue(self.manager._auto_session_running('main'))
        self.assertFalse(self.manager._auto_session_running('second'))


if __name__ == '__main__':
    unittest.main()
