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
import time
import unittest
from unittest.mock import ANY, patch

import test_cli
import test_codex_swap
import xswap_doctor as doctor
from codex_swap import SwapError, __version__, atomic_json
from codex_swap import main as codex_swap_main
from xswap_cli import (STALE_RUN_SECONDS, codex_main, codex_path_entries, enable, launch_cli,
                       link_target_path, read_settings, reconnect_wrapper, show_status, wrapper_drift)
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

    def test_a_relative_path_directory_is_not_an_entry_and_is_never_rewritten(self):
        # PATH="bin:/opt/homebrew/bin" with a project-local bin/codex shim (npm's
        # @openai/codex installs exactly that shape). The shadow repair used to wrap it:
        # xswap rewrote a link inside the user's repository and recorded `path: bin/codex`,
        # which `auto-disable` could not find again from any other working directory.
        real, updated, proxy, cli = self.wrapped_fixture()
        self.connect(proxy, cli)
        settings = read_settings(self.manager)
        project = self.base / 'project'
        (project / 'bin').mkdir(parents=True)
        shim = project / 'bin' / 'codex'
        shim.symlink_to(updated)
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)
        path = os.pathsep.join(['bin', str(cli.parent)])
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
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(LiveError) as refused:
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
