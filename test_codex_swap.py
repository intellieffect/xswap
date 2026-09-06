import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_swap import Manager, SwapError, atomic_json, main
from xswap_live import AccountPool, LiveError


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / "original"
        self.source.mkdir()
        self.source.joinpath("config.toml").write_text('model = "example"\n')
        self.auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token", "refresh_token": "fake-refresh"}}
        atomic_json(self.source / "auth.json", self.auth)
        self.manager = Manager(self.base / "store", self.source)

    def test_register_does_not_copy_or_modify_credentials(self):
        before = self.source.joinpath("auth.json").read_bytes()
        self.manager.register("main")
        self.assertEqual(self.manager.account(), ("main", self.source))
        self.assertEqual(before, self.source.joinpath("auth.json").read_bytes())
        self.assertNotIn("fake-token", self.manager.registry.read_text())
        self.assertEqual(list(self.manager.root.rglob("auth.json")), [])

    def test_two_accounts_switch_without_cross_contamination(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        self.assertFalse((second / "auth.json").exists())
        self.assertTrue((second / "config.toml").is_symlink())
        with self.assertRaises(SwapError):
            self.manager.use("second")
        atomic_json(second / "auth.json", {"auth_mode": "apikey", "OPENAI_API_KEY": "fake-second"})
        self.manager.use("second")
        self.assertEqual(self.manager.account(), ("second", second))
        # Emulate the application's atomic token refresh: the next launch uses it directly.
        atomic_json(second / "auth.json", {"auth_mode": "apikey", "OPENAI_API_KEY": "fake-refreshed"})
        self.manager.use("main")
        self.manager.use("second")
        self.assertEqual(json.loads((second / "auth.json").read_text())["OPENAI_API_KEY"], "fake-refreshed")
        self.assertEqual(json.loads((self.source / "auth.json").read_text()), self.auth)

    def test_cli_subprocess_uses_selected_home_and_no_inherited_api_key(self):
        self.manager.register("main")
        executable = self.base / "codex"
        executable.write_text('#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\nassert os.environ["CODEX_HOME"] == sys.argv[1]\nassert "OPENAI_API_KEY" not in os.environ\n')
        executable.chmod(0o700)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-inherit"}), patch.object(self.manager, "codex", return_value=str(executable)):
            self.assertEqual(self.manager.launch_cli(None, [str(self.source)]), 0)

    def test_app_uses_same_account_home_and_separate_cookie_directory(self):
        self.manager.register("main")
        app = self.base / "ChatGPT.app"
        app.mkdir()
        with patch("codex_swap.sys.platform", "darwin"), patch("codex_swap.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.manager.launch_app("main", str(app))
        command = run.call_args.args[0]
        self.assertIn(f"CODEX_HOME={self.source}", command)
        self.assertIn(f"CODEX_ELECTRON_USER_DATA_PATH={self.manager.root}/profiles/main/desktop", command)
        self.assertIn("-n", command)
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], str(self.source))

    def test_keyring_config_is_rejected_without_writes(self):
        self.source.joinpath("config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        with self.assertRaises(SwapError):
            self.manager.register("main")
        self.assertFalse(self.manager.registry.exists())

    def test_path_traversal_and_duplicate_homes_rejected(self):
        with self.assertRaises(SwapError):
            self.manager.prepare("../../escape")
        self.manager.register("main")
        with self.assertRaises(SwapError):
            self.manager.register("alias")

    def test_registry_and_profile_permissions(self):
        self.manager.prepare("second")
        self.assertEqual(self.manager.registry.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.manager.account("second")[1].stat().st_mode & 0o777, 0o700)

    def test_corrupt_registry_is_preserved(self):
        self.manager.registry.write_text("broken")
        with self.assertRaises(SwapError):
            self.manager.register("main")
        self.assertEqual(self.manager.registry.read_text(), "broken")

    def bridge_installation(self):
        package = self.base / "openclaw"
        package.mkdir()
        (package / "package.json").write_text('{"name":"openclaw"}')
        executable = package / "openclaw.mjs"
        executable.touch()
        return str(executable)

    def test_openclaw_reload_is_required_before_changing_active_account(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        executable = self.bridge_installation()
        result = {"account": "second", "completed": ["main"], "backup": "/example/backup"}
        responses = [subprocess.CompletedProcess([], 0, json.dumps(result), ""), subprocess.CompletedProcess([], 1, "", "secret-should-not-print")]
        with patch("codex_swap.shutil.which", side_effect=lambda name: executable if name == "openclaw" else "/usr/bin/node"), patch("codex_swap.subprocess.run", side_effect=responses):
            with self.assertRaisesRegex(SwapError, "saved.*reload failed"):
                self.manager.sync_openclaw("second", select=True)
        self.assertEqual(self.manager.account()[0], "main")

    def test_openclaw_dry_run_does_not_reload_or_select(self):
        self.manager.register("main")
        executable = self.bridge_installation()
        with patch("codex_swap.shutil.which", side_effect=lambda name: executable if name == "openclaw" else "/usr/bin/node"), patch("codex_swap.subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"dryRun":true}', "")) as run, contextlib.redirect_stdout(io.StringIO()):
            self.manager.sync_openclaw("main", agents=["worker"], dry=True)
        self.assertEqual(run.call_count, 1)
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["codexHome"], str(self.source))
        self.assertEqual(request["agents"], ["worker"])
        self.assertNotIn("fake-token", run.call_args.kwargs["input"])

    def test_openclaw_success_changes_default_after_reload(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        executable = self.bridge_installation()
        result = {"completed": ["main", "worker"], "backup": "/example/backup"}
        responses = [subprocess.CompletedProcess([], 0, json.dumps(result), ""), subprocess.CompletedProcess([], 0, '{"ok":true,"warningCount":0}', "")]
        with patch("codex_swap.shutil.which", side_effect=lambda name: executable if name == "openclaw" else "/usr/bin/node"), patch("codex_swap.subprocess.run", side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
            self.manager.sync_openclaw("second", select=True)
        self.assertEqual(self.manager.account()[0], "second")

    def test_login_unknown_account_raises(self):
        with self.assertRaises(SwapError):
            self.manager.login("ghost")

    def test_login_calls_codex_with_selected_home(self):
        self.manager.register("main")
        with patch("codex_swap.subprocess.call", return_value=0) as call, \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             contextlib.redirect_stdout(io.StringIO()):
            result = self.manager.login("main")
        self.assertEqual(result, 0)
        self.assertEqual(call.call_args.args[0], ["/usr/bin/codex", "login"])
        self.assertEqual(call.call_args.kwargs["env"]["CODEX_HOME"], str(self.source))

    def test_login_on_managed_profile_home(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        with patch("codex_swap.subprocess.call", return_value=0) as call, \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             contextlib.redirect_stdout(io.StringIO()):
            result = self.manager.login("second")
        self.assertEqual(result, 0)
        self.assertEqual(call.call_args.args[0], ["/usr/bin/codex", "login"])
        self.assertEqual(call.call_args.kwargs["env"]["CODEX_HOME"], str(second))

    def test_login_device_auth_flag_is_forwarded(self):
        self.manager.register("main")
        with patch("codex_swap.subprocess.call", return_value=0) as call, \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.manager.login("main", device_auth=True)
        self.assertEqual(call.call_args.args[0], ["/usr/bin/codex", "login", "--device-auth"])

    def test_add_refusal_points_to_login(self):
        self.manager.register("main")
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        stderr = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stderr(stderr):
            code = main(["add", "main"])
        self.assertEqual(code, 1)
        self.assertIn("xswap login main", stderr.getvalue())

    def test_bridge_error_does_not_forward_raw_stderr(self):
        self.manager.register("main")
        executable = self.bridge_installation()
        with patch("codex_swap.shutil.which", side_effect=lambda name: executable if name == "openclaw" else "/usr/bin/node"), patch("codex_swap.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "fake-secret-token")):
            with self.assertRaises(SwapError) as raised:
                self.manager.sync_openclaw()
        self.assertNotIn("fake-secret-token", str(raised.exception))

    def test_disable_marks_registry_and_list_shows_it(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        self.manager.set_disabled("second", True)
        self.assertTrue(json.loads(self.manager.registry.read_text())["accounts"]["second"]["disabled"])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.manager.show_accounts(offline=True)
        self.assertIn("second           ChatGPT (disabled)", out.getvalue())

    def test_disable_refuses_unknown_account(self):
        with self.assertRaises(SwapError):
            self.manager.set_disabled("ghost", True)

    def test_use_refuses_disabled_account(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        self.manager.set_disabled("second", True)
        with self.assertRaisesRegex(SwapError, "second is disabled. Run: xswap enable second"):
            self.manager.use("second")
        self.assertEqual(self.manager.account()[0], "main")

    def test_account_pool_refuses_disabled_account(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        self.manager.set_disabled("second", True)
        with self.assertRaises(LiveError):
            AccountPool(self.manager, ["main", "second"], "codex")

    def test_enable_restores_selection_and_pool_eligibility(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        self.manager.set_disabled("second", True)
        self.manager.set_disabled("second", False)
        self.assertNotIn("disabled", json.loads(self.manager.registry.read_text())["accounts"]["second"])
        self.manager.use("second")
        self.assertEqual(self.manager.account()[0], "second")
        pool = AccountPool(self.manager, ["main", "second"], "codex")
        self.assertEqual(pool.names, ["main", "second"])

    def test_json_output_reports_disabled_field(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        self.manager.set_disabled("second", True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.manager.show_accounts(offline=True, json_output=True)
        rows = {row["name"]: row for row in json.loads(out.getvalue())}
        self.assertTrue(rows["second"]["disabled"])
        self.assertFalse(rows["main"]["disabled"])

    def test_enabled_accounts_excludes_disabled(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        self.manager.set_disabled("second", True)
        self.assertEqual([name for name, _ in self.manager.enabled_accounts()], ["main"])

    def test_sync_openclaw_refuses_disabled_account_before_any_subprocess(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        self.manager.set_disabled("second", True)
        with patch("codex_swap.shutil.which") as which, patch("codex_swap.subprocess.run") as run:
            with self.assertRaisesRegex(SwapError, "second is disabled. Run: xswap enable second"):
                self.manager.sync_openclaw("second", select=True)
            with self.assertRaisesRegex(SwapError, "second is disabled. Run: xswap enable second"):
                self.manager.sync_openclaw("second")
        which.assert_not_called()
        run.assert_not_called()
        self.assertEqual(self.manager.account()[0], "main")


class MainCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.source = base / "original"
        self.source.mkdir()
        self.source.joinpath("config.toml").write_text('model = "example"\n')
        self.auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token", "refresh_token": "fake-refresh"}}
        atomic_json(self.source / "auth.json", self.auth)
        self.store = base / "store"
        patcher = patch.dict(os.environ, {"CODEX_SWAP_HOME": str(self.store), "CODEX_HOME": str(self.source)})
        patcher.start()
        self.addCleanup(patcher.stop)
        import codex_swap
        self.codex_swap = codex_swap
        with contextlib.redirect_stdout(io.StringIO()):
            self.codex_swap.main(["register", "main"])
            second = self.codex_swap.Manager().prepare("second")
        atomic_json(second / "auth.json", self.auth)

    def test_disable_and_enable_subcommands_wire_through_main(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.codex_swap.main(["disable", "second"]), 0)
        self.assertIn("Disabled second", out.getvalue())
        self.assertTrue(self.codex_swap.Manager().read()["accounts"]["second"]["disabled"])

        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.codex_swap.main(["enable", "second"]), 0)
        self.assertIn("Enabled second", out.getvalue())
        self.assertNotIn("disabled", self.codex_swap.Manager().read()["accounts"]["second"])

    def test_use_on_disabled_account_returns_error_via_main(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.codex_swap.main(["disable", "second"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.codex_swap.main(["use", "second"]), 1)
        self.assertIn("second is disabled. Run: xswap enable second", err.getvalue())


if __name__ == "__main__":
    unittest.main()

class AutoLauncherTests(unittest.TestCase):
    setUp = AccountTests.setUp

    def test_auto_launcher_uses_shared_runtime_and_explicit_pool(self):
        self.manager.register('main')
        self.manager.prepare('second')
        app = self.base / 'ChatGPT.app'
        app.mkdir()
        executable = self.base / 'codex'
        executable.touch()
        proxy = self.base / 'xswap-proxy'
        proxy.touch()
        before = self.manager.registry.read_bytes()
        with patch('codex_swap.sys.platform', 'darwin'), \
             patch('codex_swap.shutil.which', side_effect=lambda name: str(proxy if name=='xswap-proxy' else executable)), \
             patch('codex_swap.subprocess.run', return_value=subprocess.CompletedProcess([],0)) as run:
            self.manager.launch_auto_app('main,second',str(app))
        argv=run.call_args.args[0]
        self.assertIn('CODEX_APP_SERVER_FORCE_CLI=1',argv)
        self.assertIn('XSWAP_ACCOUNTS=main,second',argv)
        self.assertIn('CODEX_HOME='+str(self.manager.root/'auto/codex'),argv)
        self.assertFalse((self.manager.root/'auto/codex/auth.json').exists())
        self.assertEqual(before,self.manager.registry.read_bytes())
        self.assertEqual(self.auth,json.loads((self.source/'auth.json').read_text()))

    def test_auto_dry_run_does_not_create_runtime(self):
        self.manager.register('main')
        self.manager.prepare('second')
        app=self.base/'ChatGPT.app';app.mkdir()
        with patch('codex_swap.sys.platform','darwin'),patch('codex_swap.shutil.which',return_value='/fake/executable'),contextlib.redirect_stdout(io.StringIO()):
            self.manager.launch_auto_app('main,second',str(app),True)
        self.assertFalse((self.manager.root/'auto').exists())
