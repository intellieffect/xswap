import argparse
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_swap import Manager, atomic_json, main
import xswap_cli
from xswap_init import run_init


def scripted(*answers):
    """An injectable `ask` that returns each answer in order, then falls back to
    whatever default the caller passed once the script runs out."""
    queue = list(answers)

    def ask(prompt, default=None):
        return queue.pop(0) if queue else default

    return ask


def init_args(yes=False, no_auto=False, weekly_remaining=None):
    return argparse.Namespace(yes=yes, no_auto=no_auto, weekly_remaining=weekly_remaining)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / "original"
        self.source.mkdir()
        self.source.joinpath("config.toml").write_text('model = "example"\n')
        self.auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token", "refresh_token": "fake-refresh"}}
        self.manager = Manager(self.base / "store", self.source)

    def sign_in(self, home=None):
        atomic_json((home or self.source) / "auth.json", self.auth)

    def _write_login(self, command, env):
        # Stand-in for a real `codex login`: succeed and drop a signed-in auth.json.
        atomic_json(Path(env["CODEX_HOME"]) / "auth.json", self.auth)
        return 0

    def run_init_quiet(self, args, ask):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(Manager, "codex", return_value="/usr/bin/codex"):
            code = run_init(self.manager, args, ask=ask, interactive=True)
        return code, out.getvalue()

    # --- Step 1: register ---

    def test_register_not_yet_signed_in_is_skipped(self):
        code, out = self.run_init_quiet(init_args(), scripted())
        self.assertNotIn("main", self.manager.read()["accounts"])
        self.assertIn("register: skipped", out)

    def test_register_interactive_uses_scripted_name(self):
        self.sign_in()
        code, out = self.run_init_quiet(init_args(), scripted("primary", ""))
        self.assertIn("primary", self.manager.read()["accounts"])
        self.assertIn("register: registered primary", out)

    def test_register_yes_flag_defaults_to_main_without_prompting(self):
        self.sign_in()
        out = io.StringIO()
        # doctor's own "codex binary" check depends on what's installed on the machine
        # running the tests; init's exit code just needs to match doctor's, not be 0.
        with contextlib.redirect_stdout(out), patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             patch("xswap_doctor.check_codex_binary", return_value={"name": "codex binary", "status": "OK", "detail": "fixture"}):
            code = run_init(self.manager, init_args(yes=True))
        self.assertEqual(code, 0)
        self.assertIn("main", self.manager.read()["accounts"])
        self.assertIn("register: registered main", out.getvalue())

    def test_register_already_registered_is_skipped_second_time(self):
        self.sign_in()
        self.manager.register("main")
        code, out = self.run_init_quiet(init_args(), scripted())
        self.assertIn("register: skipped (this Codex home is already registered)", out)

    # --- Step 2: add ---

    def test_add_loop_adds_accounts_until_blank(self):
        self.sign_in()
        self.manager.register("main")
        with patch("xswap_init.subprocess.call", side_effect=self._write_login):
            code, out = self.run_init_quiet(init_args(no_auto=True), scripted("work", ""))
        accounts = self.manager.read()["accounts"]
        self.assertIn("work", accounts)
        self.assertEqual(self.manager.account()[0], "main")  # selection stays with the first account
        self.assertIn("add: added work", out)

    def test_add_yes_flag_skips_and_prints_hint(self):
        self.sign_in()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(Manager, "codex", return_value="/usr/bin/codex"):
            run_init(self.manager, init_args(yes=True))
        self.assertNotIn("work", self.manager.read()["accounts"])
        self.assertIn("xswap add work", out.getvalue())

    def test_add_failed_login_does_not_select_it(self):
        self.sign_in()
        self.manager.register("main")
        with patch("xswap_init.subprocess.call", return_value=1):
            code, out = self.run_init_quiet(init_args(no_auto=True), scripted("work", ""))
        self.assertEqual(self.manager.account()[0], "main")
        self.assertIn("codex login failed", out)

    # --- Step 3: auto ---

    def test_auto_skipped_with_one_account(self):
        self.sign_in()
        self.manager.register("main")
        code, out = self.run_init_quiet(init_args(), scripted())
        self.assertIn("auto: skipped (need at least 2 accounts; have 1)", out)

    def prepare_two_signed_in_accounts(self):
        self.sign_in()
        self.manager.register("main")
        work = self.manager.prepare("work")
        self.sign_in(work)

    def test_auto_no_answer_skips(self):
        self.prepare_two_signed_in_accounts()
        code, out = self.run_init_quiet(init_args(), scripted("", "n"))
        self.assertIn("auto: skipped (not confirmed)", out)
        self.assertFalse(xswap_cli.read_settings(self.manager).get("enabled"))

    def test_auto_skipped_by_missing_wrapper_precondition(self):
        self.prepare_two_signed_in_accounts()
        with patch("shutil.which", side_effect=lambda n: "/usr/bin/codex" if n == "codex" else None):
            code, out = self.run_init_quiet(init_args(), scripted("", "y"))
        self.assertIn("auto: skipped (codex and xswap-codex must both be installed)", out)
        self.assertFalse(xswap_cli.read_settings(self.manager).get("enabled"))

    def test_auto_yes_enables_and_wraps_codex(self):
        self.prepare_two_signed_in_accounts()
        real = self.base / "real-codex"
        real.write_text("fixture")
        real.chmod(0o700)
        proxy = self.base / "xswap-codex"
        proxy.write_text("fixture")
        proxy.chmod(0o700)
        cli = self.base / "codex"
        cli.symlink_to(real)

        def which(name):
            return str(proxy if name == "xswap-codex" else cli)

        out = io.StringIO()
        with patch("shutil.which", side_effect=which), contextlib.redirect_stdout(out):
            run_init(self.manager, init_args(), ask=scripted("", "y", "n"), interactive=True)
        self.assertTrue(xswap_cli.read_settings(self.manager)["enabled"])
        self.assertIn("auto: enabled across main, work", out.getvalue())
        self.assertEqual(cli.resolve(), proxy)

    # --- Step 4: policy ---

    def test_policy_skipped_when_auto_not_enabled(self):
        self.sign_in()
        self.manager.register("main")
        code, out = self.run_init_quiet(init_args(), scripted())
        self.assertIn("policy: skipped (automatic switching is not enabled)", out)

    def enable_auto_pool(self):
        self.prepare_two_signed_in_accounts()
        with patch.object(Manager, "codex", return_value="/usr/bin/codex"), contextlib.redirect_stdout(io.StringIO()):
            xswap_cli.enable(self.manager, "main,work", wrap=False)

    def test_policy_yes_sets_threshold(self):
        self.enable_auto_pool()
        code, out = self.run_init_quiet(init_args(no_auto=True, weekly_remaining=15), scripted("", "y"))
        self.assertEqual(xswap_cli.read_settings(self.manager)["weeklyRemainingThreshold"], 15)
        self.assertIn("policy: weekly reserve set to 15%", out)

    def test_policy_no_leaves_threshold_unset(self):
        self.enable_auto_pool()
        code, out = self.run_init_quiet(init_args(no_auto=True), scripted("", "n"))
        self.assertNotIn("weeklyRemainingThreshold", xswap_cli.read_settings(self.manager))
        self.assertIn("policy: skipped (not confirmed)", out)

    # --- --yes without a TTY, idempotency, and the empty-registry hint ---

    def test_yes_flag_runs_fully_without_a_tty(self):
        self.sign_in()
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        out = io.StringIO()
        with patch.dict(os.environ, env), patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(out), \
             patch("xswap_doctor.check_codex_binary", return_value={"name": "codex binary", "status": "OK", "detail": "fixture"}):
            code = main(["init", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("main", self.manager.read()["accounts"])

    def test_running_init_twice_changes_nothing(self):
        self.sign_in()
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        with patch.dict(os.environ, env), patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            main(["init", "--yes"])
            first = self.manager.registry.read_bytes()
            main(["init", "--yes"])
            second = self.manager.registry.read_bytes()
        self.assertEqual(first, second)

    def test_empty_registry_hint(self):
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        err = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            code = main([])
        self.assertEqual(code, 1)
        self.assertIn("run xswap init", err.getvalue())


if __name__ == "__main__":
    unittest.main()
