import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
from unittest import TestCase
from unittest.mock import patch

import test_codex_swap
import xswap_doctor as doctor
from codex_swap import atomic_json, main
from xswap_cli import disable, enable, launch_cli, read_settings, reconnect_wrapper
from xswap_relocate import canonical_codex_path, inside_root, link_packages

NEW = '0.154.0-aarch64-apple-darwin'
OLD = '0.153.4-aarch64-apple-darwin'


def install_release(standalone, name, current=False):
    """One release laid out the way scripts/install/install.sh leaves it: releases/<name>/bin/codex,
    releases/<name>/codex -> bin/codex, install.lock, and an absolute `current` link on request."""
    release = standalone / 'releases' / name
    (release / 'bin').mkdir(parents=True)
    binary = release / 'bin' / 'codex'
    binary.write_text('release ' + name)
    binary.chmod(0o755)
    (release / 'codex').symlink_to('bin/codex')
    (standalone / 'install.lock').touch()
    if current:
        link = standalone / 'current'
        if link.is_symlink():
            link.unlink()
        link.symlink_to(release)
    return binary


class RelocateTests(TestCase):
    def setUp(self):
        test_codex_swap.AccountTests.setUp(self)
        # Wrapper checks walk PATH; never let a fixture see (or re-point) this machine's real codex entries.
        self.enterContext(patch.dict(os.environ, {'PATH': str(self.base)}))

    def misplaced_install(self, enabled=True):
        """Mini on 2026-09-13: both releases inside auto/cli-codex, `current` -> 0.154.0, and
        auto.json's realCodex/originalTarget spelled through that runtime home."""
        self.manager.register('main')
        self.manager.prepare('second')
        runtime = self.manager.root / 'auto' / 'cli-codex'
        standalone = runtime / 'packages' / 'standalone'
        install_release(standalone, OLD)
        binary = install_release(standalone, NEW, current=True)
        inside = str(standalone / 'current' / 'bin' / 'codex')
        proxy = self.base / 'xswap-codex'
        proxy.write_text('fixture')
        proxy.chmod(0o700)
        cli = self.base / 'codex'
        cli.symlink_to(proxy if enabled else inside)
        atomic_json(self.manager.root / 'auto.json', {'enabled': enabled, 'accounts': ['main', 'second'], 'wrapper': {
            'path': str(cli), 'originalTarget': inside, 'realCodex': inside, 'proxy': str(proxy)}})
        return runtime, standalone, binary, inside, cli, proxy

    # --- detection helpers ---

    def test_inside_root_flags_literal_and_resolved_paths(self):
        self.manager.register('main')
        outside = self.base / 'elsewhere'
        outside.mkdir()
        (outside / 'codex').write_text('fixture')
        via_root = self.manager.root / 'auto' / 'cli-codex' / 'packages'
        via_root.parent.mkdir(parents=True)
        via_root.symlink_to(outside)
        self.assertTrue(inside_root(self.manager, str(via_root / 'codex')))  # literal path breaks with a purge
        alias = self.base / 'alias'
        alias.symlink_to(self.manager.root / 'profiles')
        self.assertTrue(inside_root(self.manager, str(alias / 'x' / 'codex')))  # resolves into the root
        self.assertFalse(inside_root(self.manager, str(outside / 'codex')))
        self.assertFalse(inside_root(self.manager, None))
        self.assertFalse(inside_root(self.manager, 'relative/codex'))
        self.assertEqual(canonical_codex_path(self.manager, str(via_root / 'standalone' / 'current' / 'bin' / 'codex')),
                         str(outside / 'standalone' / 'current' / 'bin' / 'codex'))
        self.assertEqual(canonical_codex_path(self.manager, str(outside / 'codex')), str(outside / 'codex'))

    def test_link_packages_skips_registered_external_homes_and_existing_directories(self):
        self.manager.register('main')
        self.assertEqual(link_packages(self.manager, self.source), 'outside-root')
        self.assertFalse((self.source / 'packages').exists())
        runtime = self.manager.root / 'auto' / 'cli-codex'
        (runtime / 'packages').mkdir(parents=True)
        self.assertEqual(link_packages(self.manager, runtime), 'directory')
        self.assertFalse((runtime / 'packages').is_symlink())

    def test_dangling_packages_link_is_reported_not_trusted(self):
        # install.sh starts with `mkdir -p "$STANDALONE_ROOT"`, which fails on a broken
        # link: treating it as prevention would make in-session updates die silently.
        self.manager.register('main')
        runtime = self.manager.root / 'auto' / 'cli-codex'
        runtime.mkdir(parents=True)
        (runtime / 'packages').symlink_to(self.base / 'gone')
        self.assertEqual(link_packages(self.manager, runtime), 'dangling')
        self.assertFalse((runtime / 'packages').exists())
        rows = doctor.check_packages_links(self.manager, self.manager.read()['accounts'])
        row = next(row for row in rows if row['name'] == 'packages link: auto/cli-codex')
        self.assertEqual(row['status'], 'WARN')
        self.assertIn('does not exist', row['detail'])
        self.assertIn('xswap relocate-codex', row['detail'])

    # --- prevention ---

    def test_prepare_links_a_new_profile_home_to_the_reference_packages(self):
        second = self.manager.prepare('second')
        self.assertEqual(os.readlink(second / 'packages'), str(self.source / 'packages'))
        self.assertTrue((self.source / 'packages').is_dir())
        self.assertFalse((self.source / 'packages').is_symlink())

    def test_remove_purge_does_not_follow_the_packages_link(self):
        self.manager.register('main')
        second = self.manager.prepare('second')
        atomic_json(second / 'auth.json', self.auth)
        marker = self.source / 'packages' / 'marker'
        marker.write_text('keep')
        self.manager.remove('second', purge=True)
        self.assertFalse(second.exists())
        self.assertEqual(marker.read_text(), 'keep')

    def test_cli_launch_links_the_runtime_home_to_the_reference_packages(self):
        self.manager.register('main')
        self.manager.prepare('second')

        async def fake_serve(pool, real_path, args, env, status_path, socket_path):
            return 0

        with patch.object(self.manager, 'codex', return_value='/fixture/codex'), patch('xswap_cli.serve_cli', fake_serve), \
                patch('xswap_plugins.ensure_plugins'), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(launch_cli(self.manager, 'main,second', []), 0)
        runtime = self.manager.root / 'auto' / 'cli-codex'
        self.assertEqual(os.readlink(runtime / 'packages'), str(self.source / 'packages'))
        self.assertTrue((self.source / 'packages').is_dir())

    def test_desktop_launch_links_the_auto_codex_home(self):
        self.manager.register('main')
        self.manager.prepare('second')
        app = self.base / 'ChatGPT.app'
        app.mkdir()
        executable = self.base / 'codex'
        executable.touch()
        proxy = self.base / 'xswap-proxy'
        proxy.touch()
        with patch('codex_swap.sys.platform', 'darwin'), \
                patch('codex_swap.shutil.which', side_effect=lambda name: str(proxy if name == 'xswap-proxy' else executable)), \
                patch('codex_swap.subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.manager.launch_auto_app('main,second', str(app))
        self.assertEqual(os.readlink(self.manager.root / 'auto' / 'codex' / 'packages'), str(self.source / 'packages'))

    def test_fixed_account_launch_links_a_pre_0_8_profile(self):
        self.manager.register('main')
        second = self.manager.prepare('second')
        (second / 'packages').unlink()  # a profile created before 0.8.0 has no link
        atomic_json(second / 'auth.json', self.auth)
        with patch.object(self.manager, 'codex', return_value='/fixture/codex'), \
                patch('codex_swap.subprocess.call', return_value=0):
            self.assertEqual(self.manager.launch_cli('second', ['--version']), 0)
        self.assertEqual(os.readlink(second / 'packages'), str(self.source / 'packages'))

    def test_reconnect_through_the_link_records_the_reference_path(self):
        # install.sh with CODEX_HOME=<runtime> links the visible command to
        # "$CODEX_HOME/packages/standalone/current/bin/codex" literally; through the link the
        # release lands in the reference home, and xswap must record it there.
        self.manager.register('main')
        self.manager.prepare('second')
        runtime = self.manager.root / 'auto' / 'cli-codex'
        runtime.mkdir(parents=True)
        self.assertEqual(link_packages(self.manager, runtime), 'linked')
        ref = self.source / 'packages' / 'standalone'
        install_release(ref, NEW, current=True)
        real = self.base / 'real-codex'
        real.write_text('fixture')
        real.chmod(0o700)
        proxy = self.base / 'xswap-codex'
        proxy.write_text('fixture')
        proxy.chmod(0o700)
        cli = self.base / 'codex'
        cli.symlink_to(real)

        def which(name):
            return str(proxy if name == 'xswap-codex' else cli)

        expected = str(ref / 'current' / 'bin' / 'codex')
        with patch('shutil.which', side_effect=which), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            enable(self.manager, 'main,second', wrap=True)
            cli.unlink()
            cli.symlink_to(runtime / 'packages' / 'standalone' / 'current' / 'bin' / 'codex')  # what the updater does
            self.assertEqual(self.manager.codex(), expected)
        wrapper = read_settings(self.manager)['wrapper']
        self.assertEqual(wrapper['realCodex'], expected)
        self.assertEqual(wrapper['originalTarget'], expected)
        self.assertFalse(inside_root(self.manager, wrapper['realCodex']))
        self.assertIn('reconnected', err.getvalue())
        self.assertNotIn('relocate-codex', err.getvalue())

    def test_shadowing_entry_through_the_link_records_the_reference_path(self):
        # The 2026-09-10 bypass and an in-session update together: a standalone install puts a
        # new entry ahead of the wrapped one, spelling its release through the runtime home.
        self.manager.register('main')
        self.manager.prepare('second')
        runtime = self.manager.root / 'auto' / 'cli-codex'
        runtime.mkdir(parents=True)
        self.assertEqual(link_packages(self.manager, runtime), 'linked')
        ref = self.source / 'packages' / 'standalone'
        install_release(ref, NEW, current=True)
        bin_a, bin_b = self.base / 'bin-a', self.base / 'bin-b'
        bin_a.mkdir()
        bin_b.mkdir()
        brew = self.base / 'brew-codex'
        brew.write_text('fixture')
        brew.chmod(0o700)
        proxy = self.base / 'xswap-codex'
        proxy.write_text('fixture')
        proxy.chmod(0o700)
        (bin_b / 'codex').symlink_to(brew)
        first = bin_b / 'codex'
        expected = str(ref / 'current' / 'bin' / 'codex')
        with patch('shutil.which', side_effect=lambda name: str(proxy if name == 'xswap-codex' else first)), \
                patch.dict(os.environ, {'PATH': os.pathsep.join([str(bin_a), str(bin_b)])}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            enable(self.manager, 'main,second', wrap=True)
            (bin_a / 'codex').symlink_to(runtime / 'packages' / 'standalone' / 'current' / 'bin' / 'codex')
            first = bin_a / 'codex'
            self.assertEqual(reconnect_wrapper(self.manager), expected)
        wrapper = read_settings(self.manager)['wrapper']
        self.assertEqual(wrapper['path'], str(bin_a / 'codex'))
        self.assertEqual(wrapper['realCodex'], expected)
        self.assertEqual(wrapper['originalTarget'], expected)
        self.assertFalse(inside_root(self.manager, wrapper['realCodex']))

    def test_reconnect_into_a_real_runtime_directory_names_relocate(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        real = self.base / 'real-codex'
        real.write_text('fixture')
        real.chmod(0o700)
        atomic_json(self.manager.root / 'auto.json', {'enabled': True, 'accounts': ['main', 'second'], 'wrapper': {
            'path': str(cli), 'originalTarget': str(real), 'realCodex': str(real), 'proxy': str(proxy)}})
        cli.unlink()
        cli.symlink_to(inside)  # the in-session updater on Mini, 2026-09-10
        with patch('shutil.which', side_effect=lambda name: str(cli)), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.manager.codex(), inside)
        self.assertIn('run: xswap relocate-codex', err.getvalue())
        self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'], inside)
        self.assertEqual(os.readlink(cli), str(proxy))

    # --- repair ---

    def test_relocate_moves_releases_links_runtime_and_rewrites_auto_json(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        old_bytes = binary.read_bytes()
        ref = self.source / 'packages' / 'standalone'
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['relocate-codex']), 0)
        moved = ref / 'releases' / NEW / 'bin' / 'codex'
        self.assertEqual(moved.read_bytes(), old_bytes)
        self.assertTrue((ref / 'releases' / OLD / 'bin' / 'codex').is_file())
        self.assertEqual(os.readlink(ref / 'releases' / NEW / 'codex'), 'bin/codex')
        self.assertEqual(os.readlink(ref / 'current'), str(ref / 'releases' / NEW))
        self.assertEqual(os.readlink(runtime / 'packages'), str(self.source / 'packages'))
        self.assertEqual(list(runtime.glob('.packages-relocated-*')), [])
        expected = str(ref / 'current' / 'bin' / 'codex')
        wrapper = read_settings(self.manager)['wrapper']
        self.assertEqual(wrapper['realCodex'], expected)
        self.assertEqual(wrapper['originalTarget'], expected)
        self.assertFalse(inside_root(self.manager, wrapper['realCodex']))
        self.assertEqual(Path(inside).resolve(), moved.resolve())  # a session started before the move keeps its path
        with patch('shutil.which', side_effect=lambda name: str(cli)):
            self.assertEqual(self.manager.codex(), expected)
        self.assertEqual(os.readlink(cli), str(proxy))
        self.assertIn('moved 2 release(s)', out.getvalue())
        self.assertIn(f'auto.json realCodex -> {expected}', out.getvalue())
        self.assertEqual(list(self.source.rglob('auth.json')), [self.source / 'auth.json'])

    def test_a_packages_dir_holding_more_than_the_installer_left_is_kept_not_deleted(self):
        # `_is_skeleton(aside)` is the only thing between shutil.rmtree and a directory the
        # user may still need, and it had no test: with the guard gone, a runtime `packages/`
        # holding anything the installer did not create (a stray file, a half-moved
        # releases/, a node_modules/) was renamed aside and then permanently deleted by the
        # most destructive command in this release. Keep it and say so instead.
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        # A restore that dereferenced symlinks (rsync -L, an unzipped backup) leaves
        # `current` as a real directory holding a copy of the release, which the installer
        # never creates. The layout checks accept it, so relocate() reaches the rename.
        current = standalone / 'current'
        current.unlink()
        (current / 'bin').mkdir(parents=True)
        (current / 'bin' / 'codex').write_text('restored copy')
        # The entry recorded here points straight at the release rather than through
        # `current`, so the rewritten path still resolves after the move; a `current` this
        # plan cannot place in the reference home is refused instead (next test), because the
        # record would otherwise be rewritten through a link nothing creates.
        release = standalone / 'releases' / NEW / 'bin' / 'codex'
        settings = read_settings(self.manager)
        settings['wrapper'].update(originalTarget=str(release), realCodex=str(release))
        atomic_json(self.manager.root / 'auto.json', settings)
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['relocate-codex']), 0)
        [kept] = list(runtime.glob('.packages-relocated-*'))
        self.assertEqual((kept / 'standalone' / 'current' / 'bin' / 'codex').read_text(), 'restored copy')
        self.assertIn(f'Kept {kept}', out.getvalue())
        self.assertIn('review and remove it by hand', out.getvalue())
        self.assertEqual(os.readlink(runtime / 'packages'), str(self.source / 'packages'))
        self.assertTrue((self.source / 'packages' / 'standalone' / 'releases' / NEW / 'bin' / 'codex').is_file())
        # The command doctor asks for must leave a real Codex behind, not only a kept copy.
        self.assertTrue(os.path.exists(read_settings(self.manager)['wrapper']['realCodex']))
        self.assertFalse(inside_root(self.manager, read_settings(self.manager)['wrapper']['realCodex']))

    def test_refuses_when_the_recorded_codex_runs_through_a_current_it_cannot_place(self):
        # Same dereferenced restore, but with install.sh's own record: `codex` ->
        # `<standalone>/current/bin/codex`. _current_name() reads no release name from a real
        # directory, so nothing created `current` in the reference home while _rewrite still
        # re-spelled the record through it: relocate exited 0, printed `auto.json realCodex
        # -> <ref>/packages/standalone/current/bin/codex`, and `xswap doctor` then reported
        # that path `is missing; plain codex cannot start` -- one command after the command
        # doctor had told the user to run. Refuse before anything moves, and name the repair.
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        current = standalone / 'current'
        current.unlink()
        (current / 'bin').mkdir(parents=True)
        (current / 'bin' / 'codex').write_text('restored copy')
        before = (self.manager.root / 'auto.json').read_bytes()
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['relocate-codex']), 1)
        self.assertIn(str(current), err.getvalue())
        self.assertIn('ln -sfn releases/<name> current', err.getvalue())
        self.assertIn('Nothing was changed.', err.getvalue())
        self.assertTrue(binary.is_file())
        self.assertFalse((self.source / 'packages' / 'standalone').exists())
        self.assertFalse((runtime / 'packages').is_symlink())
        self.assertEqual((self.manager.root / 'auto.json').read_bytes(), before)
        # --dry-run refuses identically: the plan is what raises.
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['relocate-codex', '--dry-run']), 1)
        self.assertIn('Nothing was changed.', err.getvalue())
        # The repair the message names makes the same command work and leaves a real Codex.
        shutil.rmtree(current)
        current.symlink_to(standalone / 'releases' / NEW)
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['relocate-codex']), 0)
        self.assertTrue(os.path.exists(read_settings(self.manager)['wrapper']['realCodex']))

    def test_relocate_rewrites_a_secondary_wrapper_record_too(self):
        # The 2026-09-10 shape: the standalone installer wrote a second `codex` ahead of the
        # wrapped one, so reconnect_wrapper made that the primary and pushed the old record
        # into `wrappers` -- still spelling the release through xswap's own root. Only the
        # primary used to be rewritten, so `auto-disable` restored that path back onto PATH
        # and told the user to run a relocate that then reported nothing to do.
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        outside = self.source / 'codex'
        outside.write_text('fixture')
        outside.chmod(0o700)
        shadow = self.base / 'local-bin'
        shadow.mkdir()
        shadow_cli = shadow / 'codex'
        shadow_cli.symlink_to(proxy)
        settings = read_settings(self.manager)
        settings['wrappers'] = [settings['wrapper']]
        settings['wrapper'] = {'path': str(shadow_cli), 'originalTarget': str(outside),
                               'realCodex': str(outside), 'proxy': str(proxy)}
        atomic_json(self.manager.root / 'auto.json', settings)
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['relocate-codex']), 0)
        expected = str(self.source / 'packages' / 'standalone' / 'current' / 'bin' / 'codex')
        record = read_settings(self.manager)['wrappers'][0]
        self.assertEqual((record['realCodex'], record['originalTarget']), (expected, expected))
        self.assertFalse(inside_root(self.manager, record['realCodex']))
        self.assertIn(f'auto.json wrappers {cli} realCodex -> {expected}', out.getvalue())
        # auto-disable restores every record; the secondary now lands outside the root, so
        # there is nothing left for it to send the user back to relocate-codex about.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            disable(self.manager)
        self.assertEqual(os.readlink(cli), expected)
        self.assertEqual(err.getvalue(), '')

    def test_dry_run_prints_the_plan_and_changes_nothing(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        before = (self.manager.root / 'auto.json').read_bytes()
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['relocate-codex', '--dry-run']), 0)
        text = out.getvalue()
        self.assertIn(f'move 2 release(s) to {self.source / "packages" / "standalone" / "releases"}', text)
        self.assertIn('Dry run: nothing changed.', text)
        self.assertFalse((runtime / 'packages').is_symlink())
        self.assertTrue(binary.is_file())
        self.assertFalse((self.source / 'packages' / 'standalone').exists())
        self.assertEqual((self.manager.root / 'auto.json').read_bytes(), before)

    def test_refuses_when_the_reference_already_holds_that_release(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        ref = self.source / 'packages' / 'standalone'
        existing = install_release(ref, NEW, current=True)
        before = (self.manager.root / 'auto.json').read_bytes()
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['relocate-codex']), 1)
        self.assertIn('already exists', err.getvalue())
        self.assertIn('Nothing was changed', err.getvalue())
        self.assertTrue(binary.is_file())
        self.assertTrue((standalone / 'releases' / OLD / 'bin' / 'codex').is_file())
        self.assertEqual(existing.read_text(), 'release ' + NEW)
        self.assertFalse((runtime / 'packages').is_symlink())
        self.assertEqual((self.manager.root / 'auto.json').read_bytes(), before)

    def test_refuses_when_two_owned_homes_hold_the_same_release(self):
        # Two surfaces updated to the same version: ctrl+u inside a bridged session left
        # 0.154.0 under auto/cli-codex, and an update under `xswap run second` left it in a
        # profile home created before 0.8.0. `destination.exists()` cannot see the plan
        # being built, so both entries claimed the same destination and the second rename
        # failed with "Directory not empty" once the first release had already moved.
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        second = self.manager.prepare('second')
        (second / 'packages').unlink()  # a profile created before 0.8.0 has no link
        duplicate = install_release(second / 'packages' / 'standalone', NEW, current=True)
        before = (self.manager.root / 'auto.json').read_bytes()
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['relocate-codex']), 1)
        self.assertIn(str(second / 'packages' / 'standalone' / 'releases' / NEW), err.getvalue())
        self.assertIn(str(standalone / 'releases' / NEW), err.getvalue())
        self.assertIn('Nothing was changed', err.getvalue())
        self.assertEqual(binary.read_text(), 'release ' + NEW)
        self.assertEqual(duplicate.read_text(), 'release ' + NEW)
        self.assertTrue((standalone / 'releases' / OLD / 'bin' / 'codex').is_file())
        self.assertFalse((self.source / 'packages' / 'standalone').exists())
        self.assertFalse((runtime / 'packages').is_symlink())
        self.assertFalse((second / 'packages').is_symlink())
        self.assertEqual((self.manager.root / 'auto.json').read_bytes(), before)

    def test_refuses_an_interrupted_install_without_moving_anything(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        (standalone / 'releases' / '.staging.123').mkdir()
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['relocate-codex']), 1)
        self.assertIn('.staging.123', err.getvalue())
        self.assertTrue(binary.is_file())
        self.assertFalse((self.source / 'packages' / 'standalone').exists())
        self.assertFalse((runtime / 'packages').is_symlink())

    def test_refuses_while_the_installer_holds_its_lock_directory(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        (standalone / 'install.lock.d').mkdir()
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['relocate-codex']), 1)
        self.assertIn('install may be in progress', err.getvalue())
        self.assertTrue(binary.is_file())

    def test_repoints_a_restored_link_when_automatic_switching_is_off(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install(enabled=False)
        self.assertEqual(os.readlink(cli), inside)
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['relocate-codex']), 0)
        expected = str(self.source / 'packages' / 'standalone' / 'current' / 'bin' / 'codex')
        self.assertEqual(os.readlink(cli), expected)
        self.assertTrue(Path(expected).is_file())
        self.assertFalse(read_settings(self.manager)['enabled'])
        self.assertIn(f'{cli} -> {expected}', out.getvalue())

    def test_a_recorded_relative_entry_is_not_repointed_against_the_working_directory(self):
        # 0.7.8 stored `shutil.which('codex')` verbatim, so a shell whose PATH held a relative
        # element recorded `bin/codex`. relocate re-points a record whose link still holds the
        # old originalTarget: resolved against the working directory that rewrote a `codex`
        # inside whatever repository relocate ran in. The record's own paths are still rewritten
        # (they are what a later disable would restore); the link is left to a human, which is
        # what doctor's relative-record FAIL asks for.
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install(enabled=False)
        settings = read_settings(self.manager)
        settings['wrapper']['path'] = 'bin/codex'
        atomic_json(self.manager.root / 'auto.json', settings)
        foreign = self.base / 'foreign-repo'
        (foreign / 'bin').mkdir(parents=True)
        shim = foreign / 'bin' / 'codex'
        shim.symlink_to(inside)  # the same target the record calls originalTarget
        cwd = os.getcwd()
        os.chdir(foreign)
        self.addCleanup(os.chdir, cwd)
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['relocate-codex']), 0)
        self.assertEqual(os.readlink(shim), inside)  # the unrelated repository is untouched
        self.assertNotIn('bin/codex ->', out.getvalue())
        expected = str(self.source / 'packages' / 'standalone' / 'current' / 'bin' / 'codex')
        record = read_settings(self.manager)['wrapper']
        self.assertEqual((record['realCodex'], record['originalTarget']), (expected, expected))

    def test_profile_home_release_is_relocated_too(self):
        self.manager.register('main')
        second = self.manager.prepare('second')
        (second / 'packages').unlink()  # a profile created before 0.8.0 has no link
        standalone = second / 'packages' / 'standalone'
        install_release(standalone, NEW, current=True)
        atomic_json(self.manager.root / 'auto.json', {})  # the codex command was never wrapped
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['relocate-codex']), 0)
        ref = self.source / 'packages' / 'standalone'
        self.assertEqual(os.readlink(second / 'packages'), str(self.source / 'packages'))
        self.assertTrue((ref / 'releases' / NEW / 'bin' / 'codex').is_file())
        self.assertEqual(os.readlink(ref / 'current'), str(ref / 'releases' / NEW))
        self.assertEqual(read_settings(self.manager), {})

    def test_keeps_the_reference_current_when_it_does_not_hold_the_real_codex(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        real = self.base / 'real-codex'
        real.write_text('fixture')
        real.chmod(0o700)
        atomic_json(self.manager.root / 'auto.json', {'enabled': True, 'accounts': ['main', 'second'], 'wrapper': {
            'path': str(cli), 'originalTarget': str(real), 'realCodex': str(real), 'proxy': str(proxy)}})
        ref = self.source / 'packages' / 'standalone'
        install_release(ref, '0.150.0-aarch64-apple-darwin', current=True)
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['relocate-codex']), 0)
        self.assertEqual(os.readlink(ref / 'current'), str(ref / 'releases' / '0.150.0-aarch64-apple-darwin'))
        self.assertTrue((ref / 'releases' / NEW / 'bin' / 'codex').is_file())
        self.assertEqual(read_settings(self.manager)['wrapper']['realCodex'], str(real))

    def test_nothing_to_relocate_says_so(self):
        self.manager.register('main')
        real = self.base / 'real-codex'
        real.write_text('fixture')
        real.chmod(0o700)
        atomic_json(self.manager.root / 'auto.json', {'enabled': True, 'accounts': ['main'], 'wrapper': {
            'path': str(self.base / 'codex'), 'originalTarget': str(real), 'realCodex': str(real),
            'proxy': str(self.base / 'xswap-codex')}})
        with patch('codex_swap.Manager', return_value=self.manager), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['relocate-codex']), 0)
        self.assertIn('Nothing to relocate', out.getvalue())
        self.assertFalse((self.source / 'packages').exists())

    def test_refuses_without_a_reference_home_outside_the_root(self):
        runtime, standalone, binary, inside, cli, proxy = self.misplaced_install()
        from codex_swap import Manager
        nested = Manager(self.manager.root, self.manager.root / 'auto' / 'cli-codex')  # CODEX_HOME inside the state dir
        with patch('codex_swap.Manager', return_value=nested), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['relocate-codex']), 1)
        self.assertIn('no reference Codex home outside', err.getvalue())
        self.assertTrue(binary.is_file())
        self.assertFalse((runtime / 'packages').is_symlink())
