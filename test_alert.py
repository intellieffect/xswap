import contextlib
import io
import os
import plistlib
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import xswap_alert
from xswap_alert import (
    AlertError,
    LABEL,
    alert_dir,
    build_plist,
    install,
    log_path,
    plist_path,
    render_run_script,
    resolve_xswap,
    run_script_path,
    status,
    uninstall,
    validate_cached,
    validate_every,
    validate_warn,
)


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ValidationTests(unittest.TestCase):
    def test_warn_accepts_1_to_100(self):
        self.assertEqual(validate_warn(1), 1.0)
        self.assertEqual(validate_warn(100), 100.0)
        self.assertEqual(validate_warn("15"), 15.0)

    def test_warn_rejects_out_of_range_and_zero(self):
        with self.assertRaises(AlertError):
            validate_warn(0)
        with self.assertRaises(AlertError):
            validate_warn(101)

    def test_every_rejects_below_one_minute(self):
        with self.assertRaises(AlertError):
            validate_every(0)
        self.assertEqual(validate_every(1), 1)

    def test_every_returns_int_and_rejects_fractional_minutes(self):
        # A fractional --every would make the crontab fallback's `*/N` output diverge
        # from the plist's exact StartInterval = every * 60; reject it instead of
        # silently rounding or truncating either side.
        self.assertEqual(validate_every(30), 30)
        self.assertIsInstance(validate_every(30), int)
        with self.assertRaises(AlertError):
            validate_every(1.5)
        with self.assertRaises(AlertError):
            validate_every("2.5")

    def test_cached_rejects_negative_but_allows_zero(self):
        with self.assertRaises(AlertError):
            validate_cached(-1)
        self.assertEqual(validate_cached(0), 0.0)


class ResolveXswapTests(unittest.TestCase):
    def test_prefers_which_result_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real-xswap"
            target.write_text("#!/bin/sh\n")
            link = Path(tmp) / "xswap"
            link.symlink_to(target)
            with patch("xswap_alert.shutil.which", return_value=str(link)):
                self.assertEqual(resolve_xswap(), str(target.resolve()))

    def test_falls_back_to_argv0_only_when_it_is_named_xswap(self):
        with patch("xswap_alert.shutil.which", return_value=None), \
             patch.object(sys, "argv", ["/some/dir/xswap"]):
            self.assertEqual(resolve_xswap(), str(Path("/some/dir/xswap").resolve()))

    def test_raises_when_not_found_and_argv0_is_not_named_xswap(self):
        with patch("xswap_alert.shutil.which", return_value=None), \
             patch.object(sys, "argv", ["/some/dir/codex_swap.py"]):
            with self.assertRaisesRegex(AlertError, "Could not find"):
                resolve_xswap()


class PlistTests(unittest.TestCase):
    def test_build_plist_carries_interval_paths_and_path_env(self):
        data = build_plist("/opt/homebrew/bin/xswap", every=30, script_path="/root/alert/run.sh",
                            out_path="/root/alert/launchd.out.log", err_path="/root/alert/launchd.err.log")
        # Round-trip through plistlib exactly like launchd would read the file.
        reloaded = plistlib.loads(plistlib.dumps(data))
        self.assertEqual(reloaded["Label"], LABEL)
        self.assertEqual(reloaded["ProgramArguments"], ["/root/alert/run.sh"])
        self.assertEqual(reloaded["StartInterval"], 1800)
        self.assertIs(reloaded["RunAtLoad"], False)
        self.assertEqual(reloaded["StandardOutPath"], "/root/alert/launchd.out.log")
        self.assertEqual(reloaded["StandardErrorPath"], "/root/alert/launchd.err.log")
        path_entries = reloaded["EnvironmentVariables"]["PATH"].split(":")
        self.assertEqual(path_entries[0], "/opt/homebrew/bin")
        for entry in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
            self.assertIn(entry, path_entries)

    def test_start_interval_rejects_would_be_sub_minute_by_construction(self):
        # `every` is validated to be >=1 minute before build_plist ever sees it (see
        # ValidationTests); build_plist itself just converts minutes to seconds.
        data = build_plist("/x/xswap", every=1, script_path="/r/run.sh", out_path="/r/o", err_path="/r/e")
        self.assertEqual(data["StartInterval"], 60)


class RunScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write_fake_xswap(self, exit_code, warn_lines):
        fake = self.dir / "fake-xswap"
        body = "#!/bin/sh\n"
        for line in warn_lines:
            # printf (unlike sh's echo) never interprets backslash escapes in its
            # argument, so the warn: line reaches stderr byte-for-byte.
            body += f"printf '%s\\n' '{line}' 1>&2\n"
        body += f"exit {exit_code}\n"
        fake.write_text(body)
        fake.chmod(0o700)
        return fake

    def write_fake_osascript(self):
        fake = self.dir / "osascript"
        record = self.dir / "osascript-calls.log"
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$2\" >> {record}\n")
        fake.chmod(0o700)
        return fake, record

    def run_script(self, script_path, extra_path_dir):
        env = dict(os.environ)
        env["PATH"] = f"{extra_path_dir}:{env.get('PATH', '')}"
        return subprocess.run(["/bin/sh", str(script_path)], env=env, capture_output=True, text=True, timeout=10)

    def test_contains_absolute_xswap_path(self):
        script = render_run_script("/abs/path/to/xswap", warn=15, cached=600, log=self.dir / "last.log")
        # Passed through shlex.quote(), not interpolated raw into a double-quoted shell
        # string: see CommandInjectionRegressionTests for why the latter is unsafe. A
        # "boring" path like this one has no characters shlex.quote() needs to escape,
        # so it comes through byte-for-byte (still shell-safe either way).
        self.assertIn(shlex.quote("/abs/path/to/xswap") + " list --warn 15 --cached 600", script)

    def test_escapes_quote_and_backslash_in_notification(self):
        warn_line = 'warn: work codex 5h 3% left (resets in 2h) has a "quote" and a \\backslash\\'
        fake_xswap = self.write_fake_xswap(3, [warn_line])
        fake_osascript, record = self.write_fake_osascript()
        log = self.dir / "last.log"
        script_text = render_run_script(str(fake_xswap), warn=15, cached=600, log=log)
        script_path = self.dir / "run.sh"
        script_path.write_text(script_text)
        script_path.chmod(0o700)

        result = self.run_script(script_path, self.dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(record.exists())
        captured = record.read_text().strip("\n")
        # The fake osascript records argv[2] verbatim: the full AppleScript source text
        # passed to `-e`. It must be syntactically valid (quotes/backslashes balanced)
        # and, once unescaped, must reproduce the original message.
        expected_message = warn_line[len("warn: "):]
        expected_applescript = (
            'display notification "'
            + expected_message.replace("\\", "\\\\").replace('"', '\\"')
            + '" with title "xswap"'
        )
        self.assertEqual(captured, expected_applescript)
        self.assertTrue(log.exists())
        self.assertEqual(oct(stat.S_IMODE(log.stat().st_mode)), oct(0o600))

    def test_non_warn_exit_code_does_not_call_osascript(self):
        fake_xswap = self.write_fake_xswap(1, [])
        fake_osascript, record = self.write_fake_osascript()
        log = self.dir / "last.log"
        script_path = self.dir / "run.sh"
        script_path.write_text(render_run_script(str(fake_xswap), warn=15, cached=600, log=log))
        script_path.chmod(0o700)

        result = self.run_script(script_path, self.dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(record.exists())
        self.assertIn("exited 1", log.read_text())

    def test_log_is_truncated_each_run(self):
        fake_xswap = self.write_fake_xswap(0, [])
        self.write_fake_osascript()
        log = self.dir / "last.log"
        log.write_text("stale content from a previous run\n" * 50)
        script_path = self.dir / "run.sh"
        script_path.write_text(render_run_script(str(fake_xswap), warn=15, cached=600, log=log))
        script_path.chmod(0o700)

        self.run_script(script_path, self.dir)
        self.assertNotIn("stale content", log.read_text())


class CommandInjectionRegressionTests(unittest.TestCase):
    """render_run_script() used to interpolate install-time paths (the resolved xswap
    binary, and the log path derived from CODEX_SWAP_HOME) into DOUBLE-quoted shell
    strings via plain str.replace(). Double quotes do not neutralise `$(...)` or
    backticks, so a path containing either was live shell syntax the moment run.sh was
    parsed -- and since launchd re-runs run.sh unattended forever, that was a persistent
    RCE primitive. The fix wraps both values in shlex.quote() (POSIX single quotes).
    These tests render with a path containing $(...), backticks, a double quote, a
    single quote, a space, and a backslash, then actually execute the script and assert
    nothing outside the intended files was ever touched.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.marker_subshell = self.dir / "REAL_PWNED_SUBSHELL"
        self.marker_backtick = self.dir / "REAL_PWNED_BACKTICK"
        # One path component carrying every dangerous character in one go: command
        # substitution, backticks, a double quote, a single quote, a space, a backslash.
        self.evil_component = (
            "evil$(touch " + self.marker_subshell.name + ")"
            "`touch " + self.marker_backtick.name + "`"
            '"double" \'single\' back\\slash and space'
        )

    def write_fake_xswap(self, path, exit_code, warn_lines):
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "#!/bin/sh\n"
        for line in warn_lines:
            body += f"printf '%s\\n' {shlex.quote(line)} 1>&2\n"
        body += f"exit {exit_code}\n"
        path.write_text(body)
        path.chmod(0o700)

    def write_fake_osascript(self, bin_dir):
        fake = bin_dir / "osascript"
        record = bin_dir / "osascript-calls.log"
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$2\" >> {shlex.quote(str(record))}\n")
        fake.chmod(0o700)
        return record

    def run_script(self, script_path, bin_dir, cwd):
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        return subprocess.run(["/bin/sh", str(script_path)], env=env, cwd=str(cwd),
                               capture_output=True, text=True, timeout=10)

    def test_malicious_xswap_and_log_paths_are_not_shell_interpreted(self):
        bin_dir = self.dir / "safe-bin"
        bin_dir.mkdir()
        record = self.write_fake_osascript(bin_dir)

        evil_dir = self.dir / self.evil_component
        xswap_bin = evil_dir / "xswap"
        self.write_fake_xswap(xswap_bin, exit_code=3, warn_lines=["warn: work codex 5h 3% left"])
        log = evil_dir / "last.log"  # evil_dir already exists: write_fake_xswap created it

        script_text = render_run_script(str(xswap_bin), warn=15, cached=600, log=log)
        script_path = self.dir / "run.sh"
        script_path.write_text(script_text)
        script_path.chmod(0o700)

        # Run with cwd=self.dir: if $(...) or backticks in the path were actually
        # executed by the shell, `touch <relative-name>` would land right here.
        result = self.run_script(script_path, bin_dir, cwd=self.dir)

        self.assertFalse(self.marker_subshell.exists(),
                          "command substitution $(...) in an interpolated path was executed")
        self.assertFalse(self.marker_backtick.exists(),
                          "backtick command substitution in an interpolated path was executed")
        self.assertEqual(result.returncode, 0, result.stderr)

        # The script still worked correctly end-to-end with the (safely quoted) exotic path.
        self.assertTrue(log.exists())
        log_text = log.read_text()
        self.assertIn("exited 3", log_text)
        self.assertTrue(record.exists())
        self.assertEqual(record.read_text().strip("\n"),
                          'display notification "work codex 5h 3% left" with title "xswap"')

    def test_malicious_path_alone_with_no_warn_output_still_runs_cleanly(self):
        bin_dir = self.dir / "safe-bin"
        bin_dir.mkdir()
        self.write_fake_osascript(bin_dir)

        evil_dir = self.dir / self.evil_component
        xswap_bin = evil_dir / "xswap"
        self.write_fake_xswap(xswap_bin, exit_code=0, warn_lines=[])
        log = evil_dir / "last.log"

        script_path = self.dir / "run.sh"
        script_path.write_text(render_run_script(str(xswap_bin), warn=15, cached=600, log=log))
        script_path.chmod(0o700)

        result = self.run_script(script_path, bin_dir, cwd=self.dir)

        self.assertFalse(self.marker_subshell.exists())
        self.assertFalse(self.marker_backtick.exists())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exited 0", log.read_text())


class InstallUninstallStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "swap-home"
        self.home = self.base / "home"
        self.home.mkdir()
        home_patch = patch("xswap_alert.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        darwin_patch = patch("xswap_alert.sys.platform", "darwin")
        darwin_patch.start()
        self.addCleanup(darwin_patch.stop)
        which_patch = patch("xswap_alert.shutil.which", return_value="/opt/homebrew/bin/xswap")
        which_patch.start()
        self.addCleanup(which_patch.stop)

    def test_dry_run_install_writes_nothing_and_never_calls_launchctl(self):
        with patch("xswap_alert.subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()) as out:
            code = install(self.root, warn=15, every=30, cached=600, dry_run=True)
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertFalse(plist_path().exists())
        self.assertFalse(run_script_path(self.root).exists())
        self.assertIn("Would write", out.getvalue())

    def test_install_writes_run_sh_and_plist_and_bootstraps(self):
        responses = [completed(returncode=1), completed(returncode=0)]  # not already loaded, then bootstrap ok
        with patch("xswap_alert.subprocess.run", side_effect=responses) as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = install(self.root, warn=20, every=15, cached=300)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][:2], ["launchctl", "print"])
        bootstrap_call = run.call_args_list[1].args[0]
        self.assertEqual(bootstrap_call[:2], ["launchctl", "bootstrap"])
        self.assertIn(str(plist_path()), bootstrap_call)
        self.assertIn("launchctl bootstrap", out.getvalue())

        plist = plist_path()
        self.assertTrue(plist.exists())
        with plist.open("rb") as fh:
            data = plistlib.load(fh)
        self.assertEqual(data["Label"], LABEL)
        self.assertEqual(data["StartInterval"], 900)
        self.assertIs(data["RunAtLoad"], False)

        script = run_script_path(self.root)
        self.assertTrue(script.exists())
        self.assertEqual(oct(stat.S_IMODE(script.stat().st_mode)), oct(0o700))
        self.assertIn("/opt/homebrew/bin/xswap", script.read_text())
        self.assertIn("--warn 20", script.read_text())
        self.assertIn("--cached 300", script.read_text())

    def test_install_when_already_loaded_boots_out_first(self):
        responses = [completed(returncode=0), completed(returncode=0), completed(returncode=0)]
        with patch("xswap_alert.subprocess.run", side_effect=responses) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            code = install(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[1].args[0][:2], ["launchctl", "bootout"])

    def test_install_raises_when_bootstrap_fails(self):
        responses = [completed(returncode=1), completed(returncode=1, stderr="boom")]
        with patch("xswap_alert.subprocess.run", side_effect=responses), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(AlertError, "boom"):
                install(self.root)

    def test_uninstall_removes_plist_and_alert_dir_and_calls_bootout(self):
        install_responses = [completed(returncode=1, stderr="not loaded"), completed(returncode=0)]
        with patch("xswap_alert.subprocess.run", side_effect=install_responses), \
             contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        self.assertTrue(plist_path().exists())
        with patch("xswap_alert.subprocess.run", return_value=completed(returncode=1, stderr="not loaded")) as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = uninstall(self.root)
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:2], ["launchctl", "bootout"])
        self.assertFalse(plist_path().exists())
        self.assertFalse(alert_dir(self.root).exists())
        self.assertIn("Removed", out.getvalue())

    def test_uninstall_dry_run_changes_nothing(self):
        install_responses = [completed(returncode=1), completed(returncode=0)]
        with patch("xswap_alert.subprocess.run", side_effect=install_responses), \
             contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        with patch("xswap_alert.subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()):
            code = uninstall(self.root, dry_run=True)
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertTrue(plist_path().exists())
        self.assertTrue(alert_dir(self.root).exists())

    def test_status_reports_absent_before_install(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = status(self.root)
        self.assertEqual(code, 0)
        self.assertIn("plist: absent", out.getvalue())
        self.assertIn("loaded: no", out.getvalue())

    def test_status_reports_present_and_loaded_with_log_tail(self):
        install_responses = [completed(returncode=1), completed(returncode=0)]
        with patch("xswap_alert.subprocess.run", side_effect=install_responses), \
             contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        log_path(self.root).write_text("\n".join(f"line {i}" for i in range(20)) + "\n")
        with patch("xswap_alert.subprocess.run", return_value=completed(returncode=0)) as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = status(self.root)
        self.assertEqual(code, 0)
        run.assert_called_once_with(["launchctl", "print", xswap_alert._bootstrap_target()],
                                     capture_output=True, text=True)
        text = out.getvalue()
        self.assertIn("plist: present", text)
        self.assertIn("loaded: yes", text)
        self.assertIn("line 19", text)
        self.assertNotIn("line 5\n", text)  # only the last 10 lines (10-19) should show


class NonDarwinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "swap-home"
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        patcher = patch("xswap_alert.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        platform_patch = patch("xswap_alert.sys.platform", "linux")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        # Deterministic regardless of what's actually on this test runner's PATH.
        which_patch = patch("xswap_alert.shutil.which", return_value="/usr/local/bin/xswap")
        which_patch.start()
        self.addCleanup(which_patch.stop)

    def test_install_prints_crontab_line_and_exits_2_without_writing(self):
        with patch("xswap_alert.subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()) as out:
            code = install(self.root, every=45)
        self.assertEqual(code, 2)
        run.assert_not_called()
        self.assertIn("*/45 * * * *", out.getvalue())
        self.assertIn("list --warn", out.getvalue())
        self.assertFalse((self.home / "Library").exists())
        self.assertFalse(alert_dir(self.root).exists())

    def test_uninstall_exits_2_without_writing(self):
        with patch("xswap_alert.subprocess.run") as run:
            code = uninstall(self.root)
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_status_exits_2(self):
        with patch("xswap_alert.subprocess.run") as run:
            code = status(self.root)
        self.assertEqual(code, 2)
        run.assert_not_called()


class MainCLIWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.store = base / "store"
        self.home = base / "home"
        self.home.mkdir()
        source = base / "codex-home"
        source.mkdir()
        env_patch = patch.dict(os.environ, {"CODEX_SWAP_HOME": str(self.store), "CODEX_HOME": str(source),
                                             "HOME": str(self.home)})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        import codex_swap
        self.codex_swap = codex_swap

    def test_requires_exactly_one_mode(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.codex_swap.main(["alert"])
        self.assertEqual(code, 1)
        self.assertIn("exactly one", err.getvalue())

        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.codex_swap.main(["alert", "--install", "--status"])
        self.assertEqual(code, 1)
        self.assertIn("exactly one", err.getvalue())

    def test_rejects_bad_warn_and_every_before_touching_disk(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.codex_swap.main(["alert", "--install", "--warn", "0", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("--warn", err.getvalue())

        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.codex_swap.main(["alert", "--install", "--warn", "101", "--dry-run"])
        self.assertEqual(code, 1)

        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.codex_swap.main(["alert", "--install", "--every", "0", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("--every", err.getvalue())

    def test_dry_run_install_wires_through_main_without_touching_launchctl(self):
        with patch("xswap_alert.sys.platform", "darwin"), \
             patch("xswap_alert.shutil.which", return_value="/opt/homebrew/bin/xswap"), \
             patch("xswap_alert.subprocess.run") as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.codex_swap.main(["alert", "--install", "--dry-run"])
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("Would write", out.getvalue())

    def test_status_wires_through_main(self):
        with patch("xswap_alert.sys.platform", "darwin"), contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.codex_swap.main(["alert", "--status"])
        self.assertEqual(code, 0)
        self.assertIn("plist: absent", out.getvalue())


if __name__ == "__main__":
    unittest.main()
