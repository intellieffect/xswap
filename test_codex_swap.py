import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
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

    def _write_login(self, command, env):
        # Stand in for a real `codex login`: succeed and drop a signed-in auth.json.
        atomic_json(Path(env["CODEX_HOME"]) / "auth.json", self.auth)
        return 0

    def test_add_use_selects_account(self):
        self.manager.register("main")
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        with patch.dict(os.environ, env), \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             patch("codex_swap.subprocess.call", side_effect=self._write_login), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["add", "work", "--use"])
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.account(), ("work", self.manager.root / "profiles" / "work" / "codex"))
        self.assertIn("Selected work.", out.getvalue())

    def test_add_without_use_leaves_existing_active(self):
        self.manager.register("main")
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        with patch.dict(os.environ, env), \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             patch("codex_swap.subprocess.call", side_effect=self._write_login), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["add", "second"])
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.account(), ("main", self.source))
        self.assertNotIn("Selected", out.getvalue())

    def test_first_add_selects_automatically(self):
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        with patch.dict(os.environ, env), \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             patch("codex_swap.subprocess.call", side_effect=self._write_login), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["add", "first"])
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.account(), ("first", self.manager.root / "profiles" / "first" / "codex"))
        self.assertIn("Selected first (first account).", out.getvalue())

    def test_prepare_only_leaves_active_none(self):
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            code = main(["add", "work", "--prepare-only"])
        self.assertEqual(code, 0)
        self.assertIsNone(self.manager.read()["active"])

    def test_failed_login_does_not_select(self):
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        with patch.dict(os.environ, env), \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             patch("codex_swap.subprocess.call", return_value=1), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(["add", "work"])
        self.assertEqual(code, 1)
        self.assertIsNone(self.manager.read()["active"])

    def test_add_use_with_aborted_browser_login_reports_error_only(self):
        # rc 0 but the user closed the browser tab before finishing: no auth.json is written.
        self.manager.register("main")
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             patch("codex_swap.subprocess.call", return_value=0), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["add", "work", "--use"])
        self.assertEqual(code, 1)
        self.assertEqual(self.manager.account(), ("main", self.source))
        self.assertNotIn("Saved", out.getvalue())
        self.assertIn("xswap:", err.getvalue())

    def test_first_add_with_aborted_browser_login_leaves_active_none(self):
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env), \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             patch("codex_swap.subprocess.call", return_value=0), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["add", "first"])
        self.assertEqual(code, 1)
        self.assertIsNone(self.manager.read()["active"])
        self.assertNotIn("Saved", out.getvalue())
        self.assertIn("xswap:", err.getvalue())

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
        self.assertIn("second", out.getvalue())
        self.assertIn("비활성화 (disabled)", out.getvalue())

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

    def test_remove_managed_without_purge_keeps_directory(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        result = self.manager.remove("second")
        self.assertEqual(result, {"removed": "second", "purged": False, "kept": str(second)})
        self.assertTrue(second.exists())
        self.assertNotIn("second", self.manager.read()["accounts"])

    def test_remove_managed_with_purge_deletes_directory(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        profile_dir = self.manager.root / "profiles" / "second"
        self.assertTrue(profile_dir.exists())
        result = self.manager.remove("second", purge=True)
        self.assertEqual(result, {"removed": "second", "purged": True, "kept": None})
        self.assertFalse(profile_dir.exists())
        self.assertNotIn("second", self.manager.read()["accounts"])

    def test_remove_registered_account_with_purge_leaves_source_home_untouched(self):
        self.manager.register("main")
        before = self.source.joinpath("auth.json").read_bytes()
        result = self.manager.remove("main", purge=True)
        self.assertEqual(result, {"removed": "main", "purged": False, "kept": str(self.source)})
        self.assertTrue(self.source.exists())
        self.assertEqual(self.source.joinpath("auth.json").read_bytes(), before)

    def test_remove_active_account_clears_active(self):
        self.manager.register("main")
        self.assertEqual(self.manager.read()["active"], "main")
        self.manager.remove("main")
        self.assertIsNone(self.manager.read()["active"])

    def test_remove_refuses_account_in_enabled_auto_pool(self):
        self.manager.register("main")
        self.manager.prepare("second")
        atomic_json(self.manager.root / "auto.json", {"enabled": True, "accounts": ["main", "second"]})
        with self.assertRaisesRegex(SwapError, "second is in the automatic switching pool"):
            self.manager.remove("second")
        self.assertIn("second", self.manager.read()["accounts"])

    def test_remove_unknown_account_refuses(self):
        with self.assertRaises(SwapError):
            self.manager.remove("ghost")

    def test_remove_purge_also_deletes_desktop_sibling(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        profile_dir = self.manager.root / "profiles" / "second"
        desktop_dir = profile_dir / "desktop"
        desktop_dir.mkdir(parents=True)
        (desktop_dir / "Cookies").write_text("fixture")
        result = self.manager.remove("second", purge=True)
        self.assertTrue(result["purged"])
        self.assertFalse(desktop_dir.exists())
        self.assertFalse(profile_dir.exists())

    def test_remove_refuses_account_used_by_running_auto_session(self):
        self.manager.register("main")
        self.manager.prepare("second")
        run_dir = self.manager.root / "auto" / "cli-runs" / "x"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(json.dumps({"account": "second"}))
        fd = os.open(run_dir / ".bridge.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaisesRegex(SwapError, "second is used by a running auto session"):
                self.manager.remove("second")
        finally:
            os.close(fd)
        self.assertIn("second", self.manager.read()["accounts"])

    def test_remove_ignores_other_accounts_running_auto_session(self):
        self.manager.register("main")
        self.manager.prepare("second")
        run_dir = self.manager.root / "auto" / "cli-runs" / "x"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text(json.dumps({"account": "main"}))
        fd = os.open(run_dir / ".bridge.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.manager.remove("second")
        finally:
            os.close(fd)
        self.assertNotIn("second", self.manager.read()["accounts"])

    def test_remove_purge_safety_failure_reports_registry_already_removed(self):
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        profile_dir = self.manager.root / "profiles" / "second"
        shutil.rmtree(profile_dir)
        external = self.base / "external-second"
        external.mkdir()
        profile_dir.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(SwapError, "second was already removed from the registry.*left untouched"):
            self.manager.remove("second", purge=True)
        self.assertNotIn("second", self.manager.read()["accounts"])
        self.assertTrue(external.exists())

    def test_map_deepest_prefix_wins(self):
        self.manager.register("main")
        self.manager.prepare("second")
        atomic_json(self.manager.account("second")[1] / "auth.json", self.auth)
        parent = self.base / "code"
        child = parent / "project"
        child.mkdir(parents=True)
        self.manager.map_dir("main", parent)
        self.manager.map_dir("second", child)
        self.assertEqual(self.manager.default_account(child), "second")
        self.assertEqual(self.manager.default_account(parent), "main")

    def test_map_sibling_name_prefix_does_not_match(self):
        self.manager.register("main")
        self.manager.prepare("second")
        atomic_json(self.manager.account("second")[1] / "auth.json", self.auth)
        base_dir = self.base / "a" / "b"
        sibling = self.base / "a" / "bc"
        base_dir.mkdir(parents=True)
        sibling.mkdir(parents=True)
        self.manager.map_dir("second", base_dir)
        self.assertEqual(self.manager.default_account(sibling), "main")

    def test_map_unmapped_cwd_falls_back_to_active(self):
        self.manager.register("main")
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.manager.default_account(elsewhere), "main")

    def test_map_explicit_name_overrides_mapping(self):
        self.manager.register("main")
        self.manager.prepare("second")
        atomic_json(self.manager.account("second")[1] / "auth.json", self.auth)
        mapped = self.base / "mapped"
        mapped.mkdir()
        self.manager.map_dir("second", mapped)
        with patch("codex_swap.os.getcwd", return_value=str(mapped)):
            self.assertEqual(self.manager.account("main")[0], "main")

    def test_map_unknown_account_raises(self):
        self.manager.register("main")
        with self.assertRaises(SwapError):
            self.manager.map_dir("ghost", self.base)

    def test_unmap_missing_raises(self):
        self.manager.register("main")
        with self.assertRaises(SwapError):
            self.manager.unmap_dir(self.base / "never-mapped")

    def test_map_removed_account_falls_back_to_active(self):
        self.manager.register("main")
        self.manager.prepare("second")
        atomic_json(self.manager.account("second")[1] / "auth.json", self.auth)
        mapped = self.base / "mapped"
        mapped.mkdir()
        self.manager.map_dir("second", mapped)
        with self.manager.locked():
            data = self.manager.read()
            del data["accounts"]["second"]
            atomic_json(self.manager.registry, data)
        self.assertEqual(self.manager.default_account(mapped), "main")

    def test_map_cli_wiring(self):
        self.manager.register("main")
        self.manager.prepare("second")
        atomic_json(self.manager.account("second")[1] / "auth.json", self.auth)
        target = self.base / "cli-mapped"
        target.mkdir()
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            code = main(["map", "second", str(target)])
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.default_account(target), "second")

    def test_map_listing_output(self):
        self.manager.register("main")
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["map"]), 0)
        self.assertIn("No directory mappings.", out.getvalue())
        target = self.base / "listed"
        target.mkdir()
        self.manager.map_dir("main", target)
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["map"]), 0)
        self.assertIn(f"{target} → main", out.getvalue())

    def test_read_rejects_non_dict_mappings(self):
        self.manager.registry.write_text(json.dumps(
            {"version": 1, "active": None, "accounts": {}, "mappings": "not-a-dict"}))
        with self.assertRaises(SwapError):
            self.manager.read()

    def test_openclaw_never_follows_directory_mapping(self):
        # Directory mappings scope a single launched session (xswap / app / status /
        # usage). OpenClaw sync mutates shared local agent state, so an omitted name
        # must resolve through the active account only, never a mapping.
        self.manager.register("main")
        second = self.manager.prepare("second")
        atomic_json(second / "auth.json", self.auth)
        mapped_dir = self.base / "work-repo"
        mapped_dir.mkdir()
        self.manager.map_dir("second", mapped_dir)
        # If sync_openclaw wrongly followed the mapping to "second", disabling it
        # would surface "second is disabled" before any subprocess call. Since the
        # active account is "main" (not disabled), it should instead reach the
        # openclaw/node PATH check.
        self.manager.set_disabled("second", True)
        with patch("codex_swap.os.getcwd", return_value=str(mapped_dir)), \
             patch("codex_swap.shutil.which", return_value=None):
            with self.assertRaisesRegex(SwapError, "OpenClaw sync requires openclaw and node in PATH."):
                self.manager.sync_openclaw()
        # The bare account() resolution (used by xswap/app/status/usage), by contrast,
        # does follow the same mapping.
        with patch("codex_swap.os.getcwd", return_value=str(mapped_dir)):
            self.assertEqual(self.manager.account(None)[0], "second")

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

    def test_remove_subcommand_wires_through_main_with_yes(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.codex_swap.main(["remove", "second", "--yes"]), 0)
        self.assertIn("Removed second", out.getvalue())
        self.assertNotIn("second", self.codex_swap.Manager().read()["accounts"])

    def test_remove_without_yes_in_non_tty_returns_error(self):
        with patch.object(self.codex_swap.sys.stdin, "isatty", return_value=False), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.codex_swap.main(["remove", "second"]), 1)
        self.assertIn("Confirm with --yes", err.getvalue())
        self.assertIn("second", self.codex_swap.Manager().read()["accounts"])

    def test_remove_interactive_prompt_accepts_lowercase_y(self):
        with patch.object(self.codex_swap.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", return_value="y"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.codex_swap.main(["remove", "second"]), 0)
        self.assertIn("Removed second", out.getvalue())
        self.assertNotIn("second", self.codex_swap.Manager().read()["accounts"])

    def test_remove_interactive_prompt_accepts_uppercase_y(self):
        with patch.object(self.codex_swap.sys.stdin, "isatty", return_value=True), \
             patch("builtins.input", return_value="Y"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.codex_swap.main(["remove", "second"]), 0)
        self.assertIn("Removed second", out.getvalue())
        self.assertNotIn("second", self.codex_swap.Manager().read()["accounts"])

    def test_remove_interactive_prompt_rejects_non_y_answers(self):
        for answer in ("n", "yes", ""):
            with self.subTest(answer=answer):
                with patch.object(self.codex_swap.sys.stdin, "isatty", return_value=True), \
                     patch("builtins.input", return_value=answer), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(self.codex_swap.main(["remove", "second"]), 1)
                self.assertIn("Aborted", out.getvalue())
                self.assertIn("second", self.codex_swap.Manager().read()["accounts"])

    def test_remove_purge_on_registered_home_prints_ignored_note(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.codex_swap.main(["remove", "main", "--purge", "--yes"]), 0)
        self.assertIn("--purge is ignored for main", out.getvalue())
        self.assertTrue(self.source.exists())


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


def dual_window_raw(remaining5h=90, remaining7d=90, reached=None, resets5h=10000, resets7d=20000):
    bucket = {"planType": "pro",
              "primary": {"usedPercent": 100 - remaining5h, "windowDurationMins": 300, "resetsAt": resets5h},
              "secondary": {"usedPercent": 100 - remaining7d, "windowDurationMins": 10080, "resetsAt": resets7d}}
    if reached:
        bucket["rateLimitReachedType"] = reached
    return {"rateLimitsByLimitId": {"codex": bucket}}


def single_window_raw(remaining, resets):
    return {"rateLimitsByLimitId": {"codex": {"planType": "pro",
            "primary": {"usedPercent": 100 - remaining, "windowDurationMins": 10080, "resetsAt": resets}}}}


class BestAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / "original"
        self.source.mkdir()
        self.source.joinpath("config.toml").write_text('model = "example"\n')
        self.auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token"}}
        atomic_json(self.source / "auth.json", self.auth)
        self.manager = Manager(self.base / "store", self.source)
        self.manager.register("main")

    def add(self, name, signed_in=True):
        home = self.manager.prepare(name)
        if signed_in:
            atomic_json(home / "auth.json", self.auth)
        return home

    def fake_read_limits(self, mapping):
        """mapping: {account name: raw response dict, or an Exception to raise}."""
        by_home = {}
        for name, value in mapping.items():
            _, home = self.manager.account(name)
            by_home[str(home)] = value

        def read(codex, env, timeout=12):
            value = by_home[env["CODEX_HOME"]]
            if isinstance(value, BaseException):
                raise value
            return value
        return read

    def test_picks_the_highest_headroom(self):
        self.add("second")
        self.add("third")
        fake = self.fake_read_limits({
            "main": dual_window_raw(remaining5h=40, remaining7d=40),
            "second": dual_window_raw(remaining5h=90, remaining7d=90),
            "third": dual_window_raw(remaining5h=60, remaining7d=60),
        })
        with patch("codex_swap.read_limits", side_effect=fake), patch.object(Manager, "codex", return_value="codex"):
            name, reason = self.manager.best_account()
        self.assertEqual(name, "second")
        self.assertEqual(reason["remaining"], {"5h": 90, "7d": 90})
        self.assertEqual({c["name"] for c in reason["candidates"]}, {"main", "second", "third"})

    def test_skips_a_bucket_with_reached(self):
        self.add("second")
        fake = self.fake_read_limits({
            "main": dual_window_raw(remaining5h=95, remaining7d=95, reached="primary"),
            "second": dual_window_raw(remaining5h=50, remaining7d=50),
        })
        with patch("codex_swap.read_limits", side_effect=fake), patch.object(Manager, "codex", return_value="codex"):
            name, _ = self.manager.best_account()
        self.assertEqual(name, "second")

    def test_skips_disabled_accounts(self):
        self.add("second")
        self.manager.set_disabled("main", True)
        fake = self.fake_read_limits({"second": dual_window_raw(remaining5h=10, remaining7d=10)})
        with patch("codex_swap.read_limits", side_effect=fake), patch.object(Manager, "codex", return_value="codex"):
            name, reason = self.manager.best_account()
        self.assertEqual(name, "second")
        self.assertEqual({c["name"] for c in reason["candidates"]}, {"second"})

    def test_skips_usage_unavailable_rows(self):
        from xswap_usage import UsageError
        self.add("second")
        fake = self.fake_read_limits({
            "main": dual_window_raw(remaining5h=70, remaining7d=70),
            "second": UsageError("usage service unavailable"),
        })
        with patch("codex_swap.read_limits", side_effect=fake), patch.object(Manager, "codex", return_value="codex"):
            name, reason = self.manager.best_account()
        self.assertEqual(name, "main")
        statuses = {c["name"]: c["status"] for c in reason["candidates"]}
        self.assertIn("usage unavailable", statuses["second"])

    def test_tie_break_on_resets_at(self):
        self.add("second")
        fake = self.fake_read_limits({
            "main": single_window_raw(50, 30000),
            "second": single_window_raw(50, 20000),
        })
        with patch("codex_swap.read_limits", side_effect=fake), patch.object(Manager, "codex", return_value="codex"):
            name, _ = self.manager.best_account()
        self.assertEqual(name, "second")

    def test_all_unknown_falls_back_to_active_with_warning(self):
        self.add("second", signed_in=False)
        fake = self.fake_read_limits({"main": {}})
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=False), \
             patch("codex_swap.read_limits", side_effect=fake), \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             patch("codex_swap.subprocess.call", return_value=0) as call, \
             contextlib.redirect_stderr(stderr):
            code = main(["run", "--best", "--", "resume"])
        self.assertEqual(code, 0)
        self.assertIn("no account with known remaining quota; using main", stderr.getvalue())
        self.assertEqual(call.call_args.args[0], ["/usr/bin/codex", "resume"])
        self.assertEqual(call.call_args.kwargs["env"]["CODEX_HOME"], str(self.source))

    def test_best_with_account_raises(self):
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=False), contextlib.redirect_stderr(stderr):
            code = main(["run", "--best", "--account", "main", "--", "resume"])
        self.assertEqual(code, 1)
        self.assertIn("--best", stderr.getvalue())

    def test_best_with_auto_raises(self):
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=False), contextlib.redirect_stderr(stderr):
            code = main(["run", "--best", "--auto", "--accounts", "main,second", "--", "resume"])
        self.assertEqual(code, 1)
        self.assertIn("--best", stderr.getvalue())

    def test_best_with_accounts_without_auto_raises(self):
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=False), contextlib.redirect_stderr(stderr):
            code = main(["run", "--best", "--accounts", "main,second", "--", "resume"])
        self.assertEqual(code, 1)
        self.assertIn("--accounts requires --auto", stderr.getvalue())

    def test_dry_run_prints_expected_json_shape(self):
        self.add("second")
        fake = self.fake_read_limits({
            "main": dual_window_raw(remaining5h=40, remaining7d=40),
            "second": dual_window_raw(remaining5h=90, remaining7d=90),
        })
        env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}
        stdout = io.StringIO()
        with patch.dict(os.environ, env, clear=False), \
             patch("codex_swap.read_limits", side_effect=fake), \
             patch.object(Manager, "codex", return_value="/usr/bin/codex"), \
             contextlib.redirect_stdout(stdout):
            code = main(["run", "--best", "--dry-run", "--", "exec", "hi"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(set(payload.keys()), {"account", "reason", "CODEX_HOME", "argv"})
        self.assertEqual(set(payload["reason"].keys()), {"remaining", "candidates"})
        self.assertEqual(payload["account"], "second")
        self.assertEqual(payload["reason"]["remaining"], {"5h": 90, "7d": 90})
        self.assertEqual({c["name"] for c in payload["reason"]["candidates"]}, {"main", "second"})
        self.assertEqual(payload["CODEX_HOME"], str(self.manager.account("second")[1]))
        self.assertEqual(payload["argv"], ["/usr/bin/codex", "exec", "hi"])


class UseBestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / "original"
        self.source.mkdir()
        self.source.joinpath("config.toml").write_text('model = "example"\n')
        self.auth = {"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token"}}
        atomic_json(self.source / "auth.json", self.auth)
        self.manager = Manager(self.base / "store", self.source)
        self.manager.register("main")
        self.env = {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}

    def add(self, name, signed_in=True):
        home = self.manager.prepare(name)
        if signed_in:
            atomic_json(home / "auth.json", self.auth)
        return home

    def fake_read_limits(self, mapping):
        """mapping: {account name: raw response dict, or an Exception to raise}."""
        by_home = {}
        for name, value in mapping.items():
            _, home = self.manager.account(name)
            by_home[str(home)] = value

        def read(codex, env, timeout=12):
            value = by_home[env["CODEX_HOME"]]
            if isinstance(value, BaseException):
                raise value
            return value
        return read

    def test_use_best_picks_and_sets_active(self):
        self.add("second")
        fake = self.fake_read_limits({
            "main": dual_window_raw(remaining5h=40, remaining7d=40),
            "second": dual_window_raw(remaining5h=100, remaining7d=88),
        })
        out = io.StringIO()
        with patch.dict(os.environ, self.env, clear=False), \
             patch("codex_swap.read_limits", side_effect=fake), \
             patch.object(Manager, "codex", return_value="codex"), \
             contextlib.redirect_stdout(out):
            code = main(["use", "--best"])
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.account()[0], "second")
        self.assertIn("Selected second (5h 100% left, 7d 88% left). CLI: xswap · Desktop: xswap app", out.getvalue())

    def test_use_best_all_unknown_exits_and_leaves_active_unchanged(self):
        fake = self.fake_read_limits({"main": {}})
        err = io.StringIO()
        with patch.dict(os.environ, self.env, clear=False), \
             patch("codex_swap.read_limits", side_effect=fake), \
             patch.object(Manager, "codex", return_value="codex"), \
             contextlib.redirect_stderr(err):
            code = main(["use", "--best"])
        self.assertEqual(code, 1)
        self.assertIn("No account with known remaining quota; nothing selected.", err.getvalue())
        self.assertEqual(self.manager.account()[0], "main")

    def test_use_without_name_or_best_fails(self):
        err = io.StringIO()
        with patch.dict(os.environ, self.env, clear=False), contextlib.redirect_stderr(err):
            code = main(["use"])
        self.assertEqual(code, 1)
        self.assertIn("Give an account name or --best.", err.getvalue())

    def test_use_name_and_best_together_fails(self):
        self.add("second")
        err = io.StringIO()
        with patch.dict(os.environ, self.env, clear=False), contextlib.redirect_stderr(err):
            code = main(["use", "second", "--best"])
        self.assertEqual(code, 1)
        self.assertIn("Give an account name or --best.", err.getvalue())

    def test_switch_best_alias_works(self):
        self.add("second")
        fake = self.fake_read_limits({
            "main": dual_window_raw(remaining5h=40, remaining7d=40),
            "second": dual_window_raw(remaining5h=100, remaining7d=88),
        })
        with patch.dict(os.environ, self.env, clear=False), \
             patch("codex_swap.read_limits", side_effect=fake), \
             patch.object(Manager, "codex", return_value="codex"), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(["switch", "--best"])
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.account()[0], "second")

    def test_use_best_openclaw_syncs_the_chosen_account(self):
        self.add("second")
        fake = self.fake_read_limits({
            "main": dual_window_raw(remaining5h=40, remaining7d=40),
            "second": dual_window_raw(remaining5h=100, remaining7d=88),
        })
        with patch.dict(os.environ, self.env, clear=False), \
             patch("codex_swap.read_limits", side_effect=fake), \
             patch.object(Manager, "codex", return_value="codex"), \
             patch.object(Manager, "sync_openclaw") as sync_openclaw, \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(["use", "--best", "--openclaw"])
        self.assertEqual(code, 0)
        sync_openclaw.assert_called_once_with("second", select=True)

    def test_use_model_without_best_fails(self):
        self.add("second")
        err = io.StringIO()
        with patch.dict(os.environ, self.env, clear=False), contextlib.redirect_stderr(err):
            code = main(["use", "second", "--model", "gpt-5"])
        self.assertEqual(code, 1)
        self.assertIn("--model requires --best.", err.getvalue())
        self.assertEqual(self.manager.account()[0], "main")

    def test_use_best_omits_parenthetical_when_remaining_is_unknown(self):
        out = io.StringIO()
        with patch.dict(os.environ, self.env, clear=False), \
             patch.object(Manager, "best_account", return_value=("main", {"remaining": {"5h": None, "7d": None}, "candidates": []})), \
             contextlib.redirect_stdout(out):
            code = main(["use", "--best"])
        self.assertEqual(code, 0)
        self.assertIn("Selected main. CLI: xswap · Desktop: xswap app", out.getvalue())
        self.assertNotIn("(", out.getvalue())
