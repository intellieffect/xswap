import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_swap import Manager, atomic_json, main
import xswap_doctor as doctor


def fake_jwt(exp=None, claims=None):
    header = base64.urlsafe_b64encode(b"{}").rstrip(b"=")
    body = dict(claims or {})
    if exp is not None:
        body["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=")
    return (header + b"." + payload + b".fixture-sig").decode()


def find(results, name):
    for row in results:
        if row["name"] == name:
            return row
    raise AssertionError(f"no check named {name!r} in {[r['name'] for r in results]}")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.source.joinpath("config.toml").write_text('model = "example"\n')
        self.manager = Manager(self.base / "store", self.source)
        # A nonexistent-by-default state dir: doctor's openclaw-cooldown check must
        # never fall through to this developer machine's real ~/.openclaw database.
        self.enterContext(patch.dict(os.environ, {"OPENCLAW_STATE_DIR": str(self.base / "no-openclaw-here")}))

    def write_auth(self, token=None, exp=None, extra=None):
        data = {"tokens": {"access_token": token or fake_jwt(exp), "refresh_token": "fixture-refresh"}}
        if extra:
            data.update(extra)
        else:
            data["auth_mode"] = "chatgpt"
        atomic_json(self.source / "auth.json", data)

    def register_main(self, exp=None):
        self.write_auth(exp=exp if exp is not None else time.time() + 100000)
        self.manager.register("main")

    def run_doctor(self, codex="/fixture/codex", codex_version="1.2.3", path_dir=None):
        def which(name):
            if name == "codex":
                return codex
            return None

        def run(cmd, **kwargs):
            class Result:
                returncode = 0
                stdout = codex_version
                stderr = ""
            return Result()

        with patch("shutil.which", side_effect=which), patch("subprocess.run", side_effect=run), \
                patch.dict(os.environ, {"PATH": str(path_dir or self.base)}):
            return doctor.run(self.manager)

    def doctor_wrapper_row(self, settings, accounts=None, path_dir=None):
        """check_wrapper with a pinned PATH so the row never depends on this machine."""
        return doctor.check_wrapper(settings, accounts or {}, env={"PATH": str(path_dir or self.base)})

    # --- token expiry ---

    def test_token_expiry_future_is_ok(self):
        self.register_main(exp=time.time() + 100000)
        row = find(self.run_doctor(), "main: token expiry")
        self.assertEqual(row["status"], "OK")

    def test_token_expiry_under_24h_warns(self):
        self.register_main(exp=time.time() + 3600)
        row = find(self.run_doctor(), "main: token expiry")
        self.assertEqual(row["status"], "WARN")

    def test_token_expiry_past_fails(self):
        self.register_main(exp=time.time() - 3600)
        row = find(self.run_doctor(), "main: token expiry")
        self.assertEqual(row["status"], "FAIL")

    # --- missing / broken credentials ---

    def test_missing_auth_json_fails_credentials_check(self):
        self.register_main()
        (self.source / "auth.json").unlink()
        results = self.run_doctor()
        row = find(results, "main: credentials")
        self.assertEqual(row["status"], "FAIL")
        # No readable token, so the expiry sub-check must not also fire.
        self.assertFalse(any(r["name"] == "main: token expiry" for r in results))

    def test_unsafe_auth_json_permissions_fail_with_read_auth_message(self):
        self.register_main()
        (self.source / "auth.json").chmod(0o644)
        row = find(self.run_doctor(), "main: credentials")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("auth.json", row["detail"])

    # --- plugins ---

    def test_plugins_symlink_warns(self):
        self.register_main()
        outside = self.base / "elsewhere"
        outside.mkdir()
        (self.source / "plugins").symlink_to(outside, target_is_directory=True)
        row = find(self.run_doctor(), "main: plugins")
        self.assertEqual(row["status"], "WARN")
        self.assertIn("repair-plugins", row["detail"])

    def test_plugins_real_dir_is_ok(self):
        self.register_main()
        (self.source / "plugins").mkdir()
        row = find(self.run_doctor(), "main: plugins")
        self.assertEqual(row["status"], "OK")

    def test_plugins_absent_is_ok(self):
        self.register_main()
        row = find(self.run_doctor(), "main: plugins")
        self.assertEqual(row["status"], "OK")
        self.assertIn("no plugins", row["detail"])

    # --- credential store mode ---

    def test_keyring_store_fails(self):
        home = self.base / "keyring-home"
        home.mkdir()
        home.joinpath("config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
        manager = Manager(self.base / "keyring-store", home)
        row = find(doctor.run(manager), "credential store")
        self.assertEqual(row["status"], "FAIL")

    # --- disabled accounts never fail the run ---

    def test_disabled_account_missing_auth_warns_and_exit_is_0(self):
        self.register_main()
        self.manager.prepare("second")  # home created, no auth.json signed in
        self.manager.set_disabled("second", True)
        results = self.run_doctor()
        row = find(results, "second: credentials")
        self.assertEqual(row["status"], "WARN")
        self.assertIn("disabled", row["detail"])
        self.assertFalse(any(r["status"] == "FAIL" for r in results))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.print_report(results), 0)

    def test_disabled_account_expired_token_warns_not_fails(self):
        self.register_main()
        second_home = self.manager.prepare("second")
        atomic_json(second_home / "auth.json", {"auth_mode": "chatgpt",
            "tokens": {"access_token": fake_jwt(exp=time.time() - 3600), "refresh_token": "fixture-refresh"}})
        self.manager.set_disabled("second", True)
        results = self.run_doctor()
        row = find(results, "second: token expiry")
        self.assertEqual(row["status"], "WARN")
        self.assertIn("disabled", row["detail"])
        self.assertFalse(any(r["status"] == "FAIL" for r in results))

    def test_disabled_account_missing_home_warns_not_fails(self):
        self.register_main()
        second_home = self.manager.prepare("second")
        import shutil as shutil_module
        shutil_module.rmtree(second_home)
        self.manager.set_disabled("second", True)
        results = self.run_doctor()
        row = find(results, "second: home")
        self.assertEqual(row["status"], "WARN")
        self.assertFalse(any(r["status"] == "FAIL" for r in results))

    def test_enabled_account_missing_auth_still_fails(self):
        self.register_main()
        self.manager.prepare("second")
        results = self.run_doctor()
        row = find(results, "second: credentials")
        self.assertEqual(row["status"], "FAIL")

    # --- corrupted auto.json: one FAIL, not duplicated across wrapper/auto pool ---

    def test_corrupted_auto_json_single_fail_and_skips_wrapper_and_pool(self):
        self.register_main()
        (self.manager.root / "auto.json").write_text("not json")
        results = self.run_doctor()
        names = [r["name"] for r in results]
        self.assertEqual(find(results, "auto settings")["status"], "FAIL")
        self.assertNotIn("wrapper", names)
        self.assertNotIn("auto pool", names)
        # Only one row reports this cause.
        self.assertEqual(names.count("auto settings"), 1)

    # --- check_auto_dir ---

    def test_check_auto_dir_reports_umask_mode_with_fix(self):
        import types
        manager = types.SimpleNamespace(root=self.base / "store")
        self.assertEqual(doctor.check_auto_dir(manager)["status"], "OK")
        auto = manager.root / "auto"
        auto.mkdir(parents=True)
        auto.chmod(0o755)
        row = doctor.check_auto_dir(manager)
        self.assertEqual(row["status"], "WARN")
        self.assertIn(f"chmod 700 {auto}", row["detail"])
        auto.chmod(0o700)
        self.assertEqual(doctor.check_auto_dir(manager)["status"], "OK")

    # --- check_wrapper ---

    def test_check_wrapper_not_connected_is_ok(self):
        row = self.doctor_wrapper_row({})
        self.assertEqual(row["status"], "OK")
        self.assertEqual(self.doctor_wrapper_row({}, {"main": {}})["status"], "OK")
        # A disabled account does not count toward the two needed for switching.
        self.assertEqual(self.doctor_wrapper_row({}, {"main": {}, "old": {"disabled": True}})["status"], "OK")

    def test_check_wrapper_not_connected_with_two_accounts_warns(self):
        row = self.doctor_wrapper_row({}, {"main": {}, "work": {}})
        self.assertEqual(row["status"], "WARN")
        self.assertIn("xswap auto-enable --accounts main,work --wrap-codex", row["detail"])

    def test_check_wrapper_matches_proxy_is_ok(self):
        # "enabled": True matters: a dormant record reads as "not connected", not as connected.
        settings, bin_dir, link = self.wrapped_entry(self.executable("xswap-codex"))
        row = self.doctor_wrapper_row(settings, path_dir=bin_dir)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["detail"], f"{link} -> xswap-codex")

    def wrapped_entry(self, target, enabled=True, name="codex"):
        """A recorded wrapper whose codex entry currently points at `target` (dangling allowed).

        The entry is `<bin>/codex` so a PATH pinned at `bin` actually finds it; that is
        what makes "why was this entry left alone" the row the test is reading.
        """
        bin_dir = self.base / "bin"
        bin_dir.mkdir(exist_ok=True)
        proxy = self.executable("xswap-codex")
        link = bin_dir / name
        link.symlink_to(target)
        settings = {"enabled": enabled, "accounts": ["main", "work"],
                    "wrapper": {"path": str(link), "proxy": str(proxy),
                                "originalTarget": str(proxy), "realCodex": str(proxy)}}
        return settings, bin_dir, link

    def executable(self, name):
        path = self.base / name
        path.write_text("fixture")
        path.chmod(0o700)
        return path

    def test_check_wrapper_replaced_by_update_names_the_reconnect_command(self):
        # A user-owned link re-pointed at another executable while switching is on: plain codex
        # is bypassing the selection right now (FAIL), and the next launch, list, or use
        # reconnects it -- doctor still names the manual command.
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        proxy = self.executable("xswap-codex")
        link = bin_dir / "codex"
        link.symlink_to(self.executable("release-codex"))
        settings = {"enabled": True, "accounts": ["main", "work"],
                    "wrapper": {"path": str(link), "proxy": str(proxy),
                                "originalTarget": str(proxy), "realCodex": str(proxy)}}
        row = self.doctor_wrapper_row(settings, {"main": {}, "work": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("xswap auto-enable --accounts main,work --wrap-codex", row["detail"])
        self.assertIn("next xswap launch, list, or use reconnects it", row["detail"])

    # --- check_wrapper: conditions xswap deliberately leaves alone (INT-5186 item 10) ---

    def test_check_wrapper_dangling_target_names_reason_and_manual_fix(self):
        # A dangling entry is not runnable, so a PATH lookup finds no codex at all: the row
        # leads with that and still names the reason the entry was left alone.
        gone = self.base / "gone"
        settings, bin_dir, link = self.wrapped_entry(gone)
        row = self.doctor_wrapper_row(settings, {"main": {}, "work": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn(f"no codex on PATH and the wrapped entry {link} no longer runs (dangling-target): ", row["detail"])
        self.assertIn(f"{link} -> {gone} does not exist, so xswap leaves it alone. Fix: reinstall Codex", row["detail"])
        self.assertIn("then run: xswap auto-enable --accounts main,work --wrap-codex", row["detail"])
        self.assertNotIn("reconnects it", row["detail"])
        self.assertEqual(os.readlink(link), str(gone))  # doctor stays read-only

    def test_check_wrapper_not_executable_target(self):
        notes = self.base / "notes.txt"
        notes.write_text("fixture")
        settings, bin_dir, link = self.wrapped_entry(notes)
        row = self.doctor_wrapper_row(settings, {"main": {}, "work": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn(f"no codex on PATH and the wrapped entry {link} no longer runs (not-executable): ", row["detail"])
        self.assertIn(f"{link} -> {notes} is not an executable file", row["detail"])
        self.assertIn("Fix: point the link at a Codex executable, then run: xswap auto-enable", row["detail"])

    def test_check_wrapper_regular_file_where_the_link_was(self):
        # An executable regular file does run as plain `codex`, so the entry is the one
        # bypassing the selection; the row says why xswap will not replace it.
        settings, bin_dir, link = self.wrapped_entry(self.executable("release-codex"))
        link.unlink()
        link.write_text("fixture")
        link.chmod(0o700)
        row = self.doctor_wrapper_row(settings, {"main": {}, "work": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("codex entry changed outside xswap (not-a-symlink): ", row["detail"])
        self.assertIn(f"{link} is not a symlink, so xswap leaves it alone. Fix: replace it with a user-owned symlink", row["detail"])

    def test_check_wrapper_missing_entry(self):
        settings, bin_dir, link = self.wrapped_entry(self.executable("release-codex"))
        link.unlink()
        row = self.doctor_wrapper_row(settings, {"main": {}, "work": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn(f"no codex on PATH and the wrapped entry {link} no longer runs (missing): ", row["detail"])
        self.assertIn(f"{link} does not exist. Fix: reinstall Codex or recreate the link, then run: xswap auto-enable", row["detail"])

    def test_check_wrapper_foreign_owner(self):
        settings, bin_dir, link = self.wrapped_entry(self.executable("release-codex"))
        with patch("os.getuid", return_value=os.getuid() + 1):
            row = self.doctor_wrapper_row(settings, {"main": {}, "work": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("codex entry changed outside xswap (not-user-owned): ", row["detail"])
        self.assertIn(f"{link} is not owned by this user, so xswap leaves it alone. Fix: make it a user-owned symlink", row["detail"])

    def test_check_wrapper_unreadable_entry(self):
        settings, bin_dir, link = self.wrapped_entry(self.executable("release-codex"))
        with patch("os.readlink", side_effect=PermissionError(13, "Permission denied")):
            row = self.doctor_wrapper_row(settings, {"main": {}, "work": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn(f"no codex on PATH and the wrapped entry {link} no longer runs (unreadable): ", row["detail"])
        self.assertIn(f"{link} could not be inspected (Permission denied)", row["detail"])
        self.assertNotIn("fixture", row["detail"])

    def test_check_wrapper_auto_disabled_reads_like_not_connected(self):
        # After `xswap auto-disable` the wrapper record stays and the link points at the
        # release; nothing reconnects it, so say so, WARN only once two accounts could switch.
        release = self.executable("release-codex")
        settings, bin_dir, link = self.wrapped_entry(release, enabled=False)
        row = self.doctor_wrapper_row(settings, {"main": {}, "work": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "WARN")
        self.assertIn("(auto-disabled): ", row["detail"])
        self.assertIn(f"automatic switching is disabled, so xswap leaves {link} alone (-> {release})", row["detail"])
        self.assertIn("Fix: enable automatic switching and connect it: xswap auto-enable --accounts main,work --wrap-codex", row["detail"])
        self.assertNotIn("changed outside xswap", row["detail"])
        row = self.doctor_wrapper_row(settings, {"main": {}}, path_dir=bin_dir)
        self.assertEqual(row["status"], "OK")
        self.assertIn("(auto-disabled): ", row["detail"])
        self.assertIn("xswap auto-enable --accounts main --wrap-codex", row["detail"])

    def test_doctor_run_reports_wrapper_skip_reason_and_leaves_the_link_alone(self):
        self.register_main()
        gone = self.base / "gone"
        settings, bin_dir, link = self.wrapped_entry(gone)
        atomic_json(self.manager.root / "auto.json", settings)
        results = self.run_doctor(path_dir=bin_dir)
        row = find(results, "wrapper")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("(dangling-target): ", row["detail"])
        self.assertEqual(os.readlink(link), str(gone))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.print_report(results), 1)  # auto pool: "work" is not registered
        self.assertEqual(find(results, "auto pool")["status"], "FAIL")

    # --- auto pool ---

    def test_auto_pool_unknown_name_fails(self):
        self.register_main()
        atomic_json(self.manager.root / "auto.json", {"enabled": True, "accounts": ["main", "ghost"]})
        row = find(self.run_doctor(), "auto pool")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("ghost", row["detail"])

    def test_auto_pool_disabled_member_fails(self):
        self.register_main()
        self.manager.prepare("second")
        self.manager.set_disabled("second", True)
        atomic_json(self.manager.root / "auto.json", {"enabled": True, "accounts": ["main", "second"]})
        row = find(self.run_doctor(), "auto pool")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("second", row["detail"])

    def test_auto_pool_not_enabled_is_ok(self):
        self.register_main()
        row = find(self.run_doctor(), "auto pool")
        self.assertEqual(row["status"], "OK")

    # --- codex binary ---

    def test_codex_missing_fails(self):
        self.register_main()
        with patch("shutil.which", return_value=None):
            row = find(doctor.run(self.manager), "codex binary")
        self.assertEqual(row["status"], "FAIL")

    def test_codex_missing_exits_1_via_main(self):
        self.register_main()
        with patch.dict(os.environ, {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}), \
                patch("shutil.which", return_value=None), contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("codex binary", out.getvalue())

    def test_all_ok_exits_0(self):
        self.register_main()
        with patch.dict(os.environ, {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}), \
                patch("shutil.which", side_effect=lambda n: "/fixture/codex" if n == "codex" else None), \
                patch("subprocess.run", return_value=type("R", (), {"returncode": 0, "stdout": "0.1.0", "stderr": ""})()), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main(["doctor"])
        self.assertEqual(code, 0)

    # --- openclaw ---

    def test_openclaw_absent_warns(self):
        self.register_main()
        with patch("shutil.which", side_effect=lambda n: "/fixture/codex" if n == "codex" else None):
            row = find(doctor.run(self.manager), "openclaw")
        self.assertEqual(row["status"], "WARN")

    # --- real codex location and packages links (INT-5186 item 2) ---

    def wrapped_settings(self, real, original=None):
        atomic_json(self.manager.root / "auto.json", {"enabled": True, "accounts": ["main"], "wrapper": {
            "path": str(self.base / "codex-link"), "originalTarget": original or real, "realCodex": real,
            "proxy": "/fixture/xswap-codex"}})

    def test_real_codex_inside_root_fails_and_names_relocate(self):
        self.register_main()
        inside = self.manager.root / "auto" / "cli-codex" / "packages" / "standalone" / "current" / "bin" / "codex"
        inside.parent.mkdir(parents=True)
        inside.write_text("fixture")
        self.wrapped_settings(str(inside))
        before = (self.manager.root / "auto.json").read_bytes()
        results = self.run_doctor()
        row = find(results, "real codex")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("xswap relocate-codex", row["detail"])
        self.assertIn(str(inside), row["detail"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(doctor.print_report(results), 1)
        # Read-only: nothing was moved, linked, or rewritten.
        self.assertTrue(inside.is_file())
        self.assertFalse((self.manager.root / "auto" / "cli-codex" / "packages").is_symlink())
        self.assertEqual((self.manager.root / "auto.json").read_bytes(), before)

    def test_real_codex_resolving_into_root_through_a_link_fails(self):
        self.register_main()
        target = self.manager.root / "profiles" / "x" / "codex" / "bin" / "codex"
        target.parent.mkdir(parents=True)
        target.write_text("fixture")
        alias = self.base / "alias"
        alias.symlink_to(self.manager.root / "profiles")
        self.wrapped_settings(str(alias / "x" / "codex" / "bin" / "codex"))
        self.assertEqual(find(self.run_doctor(), "real codex")["status"], "FAIL")

    def test_real_codex_missing_fails_with_recovery_steps(self):
        self.register_main()
        gone = self.base / "gone" / "codex"
        self.wrapped_settings(str(gone))
        row = find(self.run_doctor(), "real codex")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("is missing", row["detail"])
        self.assertIn("xswap auto-enable --accounts main --wrap-codex", row["detail"])

    def test_real_codex_outside_root_is_ok(self):
        self.register_main()
        real = self.base / "real-codex"
        real.write_text("fixture")
        self.wrapped_settings(str(real))
        row = find(self.run_doctor(), "real codex")
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["detail"], str(real))

    def test_real_codex_original_target_inside_root_fails_even_when_real_is_outside(self):
        self.register_main()
        real = self.base / "real-codex"
        real.write_text("fixture")
        self.wrapped_settings(str(real), original=str(self.manager.root / "profiles" / "x" / "codex" / "bin" / "codex"))
        self.assertEqual(find(self.run_doctor(), "real codex")["status"], "FAIL")

    def test_real_codex_row_omitted_without_wrapper(self):
        self.register_main()
        self.assertFalse(any(r["name"] == "real codex" for r in self.run_doctor()))

    def test_packages_link_directory_warns_and_link_is_ok(self):
        self.register_main()
        runtime = self.manager.root / "auto" / "cli-codex"
        (runtime / "packages").mkdir(parents=True)
        row = find(self.run_doctor(), "packages link: auto/cli-codex")
        self.assertEqual(row["status"], "WARN")
        self.assertIn("xswap relocate-codex", row["detail"])
        (runtime / "packages").rmdir()
        (self.source / "packages").mkdir()  # what link_packages creates before it links
        (runtime / "packages").symlink_to(self.source / "packages")
        results = self.run_doctor()
        row = find(results, "packages link: auto/cli-codex")
        self.assertEqual(row["status"], "OK")
        self.assertIn(str(self.source / "packages"), row["detail"])
        self.assertFalse(any(r["name"] == "packages link: auto/codex" for r in results))

    def test_packages_link_into_the_root_warns(self):
        self.register_main()
        runtime = self.manager.root / "auto" / "cli-codex"
        runtime.mkdir(parents=True)
        (self.manager.root / "auto" / "codex" / "packages").mkdir(parents=True)
        (runtime / "packages").symlink_to(self.manager.root / "auto" / "codex" / "packages")
        row = find(self.run_doctor(), "packages link: auto/cli-codex")
        self.assertEqual(row["status"], "WARN")
        self.assertIn("stays inside", row["detail"])

    def test_packages_link_absent_runtime_home_is_ok(self):
        self.register_main()
        (self.manager.root / "auto" / "cli-codex").mkdir(parents=True)
        row = find(self.run_doctor(), "packages link: auto/cli-codex")
        self.assertEqual(row["status"], "OK")
        self.assertIn("not created yet", row["detail"])

    # --- storage / registry ---

    def test_storage_dir_mode_ok(self):
        self.register_main()
        row = find(self.run_doctor(), "storage")
        self.assertEqual(row["status"], "OK")

    def test_registry_parses_ok(self):
        self.register_main()
        row = find(self.run_doctor(), "registry")
        self.assertEqual(row["status"], "OK")

    def test_invalid_registry_reported_as_fail_without_crashing(self):
        self.register_main()
        self.manager.registry.write_text("not json")
        results = self.run_doctor()
        row = find(results, "registry")
        self.assertEqual(row["status"], "FAIL")

    # --- --json shape ---

    def test_json_output_shape(self):
        self.register_main()
        with patch.dict(os.environ, {"CODEX_SWAP_HOME": str(self.manager.root), "CODEX_HOME": str(self.source)}), \
                patch("shutil.which", return_value=None), contextlib.redirect_stdout(io.StringIO()) as out:
            main(["doctor", "--json"])
        parsed = json.loads(out.getvalue())
        self.assertIsInstance(parsed, list)
        for row in parsed:
            self.assertEqual(set(row), {"name", "status", "detail"})
            self.assertIn(row["status"], ("OK", "WARN", "FAIL"))

    # --- secret env stripped before spawning probes ---

    def test_codex_version_probe_strips_secret_env(self):
        self.register_main()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs.get("env") or {})
            class Result:
                returncode = 0
                stdout = "1.0.0"
                stderr = ""
            return Result()

        with patch.dict(os.environ, {"OPENAI_API_KEY": "leak", "CODEX_WORKLOAD_IDENTITY_X": "leak"}), \
                patch("shutil.which", side_effect=lambda n: "/fixture/codex" if n == "codex" else None), \
                patch("subprocess.run", side_effect=fake_run):
            doctor.check_codex_binary()
        self.assertNotIn("OPENAI_API_KEY", captured)
        self.assertNotIn("CODEX_WORKLOAD_IDENTITY_X", captured)

    def test_openclaw_probe_strips_secret_env(self):
        package_root = self.base / "openclaw-pkg"
        package_root.mkdir()
        (package_root / "package.json").write_text(json.dumps({"name": "openclaw"}))
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs.get("env") or {})
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""
            return Result()

        with patch.dict(os.environ, {"CODEX_API_KEY": "leak"}), \
                patch("shutil.which", side_effect=lambda n: str(self.base / n) if n in ("openclaw", "node") else None), \
                patch("xswap_doctor.resolve_openclaw_package_root", return_value=package_root), \
                patch("subprocess.run", side_effect=fake_run):
            doctor.check_openclaw()
        self.assertNotIn("CODEX_API_KEY", captured)

    def test_never_prints_raw_token(self):
        token = fake_jwt(exp=time.time() + 100000)
        self.register_main()
        self.write_auth(token=token)
        results = self.run_doctor()
        blob = json.dumps(results)
        self.assertNotIn(token, blob)
        self.assertNotIn("fixture-refresh", blob)


class WrapperPathTests(unittest.TestCase):
    """`xswap doctor`'s wrapper row against a fixture PATH of two bin directories.

    bin-b/codex is the entry xswap wrapped (originally -> a brew release); tests put
    other `codex` entries in bin-a and order PATH to reproduce 2026-09-10."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.bin_a, self.bin_b, self.empty = self.base / "bin-a", self.base / "bin-b", self.base / "empty"
        for directory in (self.bin_a, self.bin_b, self.empty):
            directory.mkdir()
        self.standalone = self.executable("standalone/bin/codex")
        self.brew = self.executable("Cellar/codex/0.150.0/bin/codex")
        self.proxy = self.executable("tools/xswap-codex")
        (self.bin_b / "codex").symlink_to(self.proxy)
        self.settings = {"enabled": True, "accounts": ["main", "work"],
                         "wrapper": {"path": str(self.bin_b / "codex"), "originalTarget": str(self.brew),
                                     "realCodex": str(self.brew), "proxy": str(self.proxy)}}
        self.accounts = {"main": {}, "work": {}}
        self.reconnect = "xswap auto-enable --accounts main,work --wrap-codex"

    def executable(self, relative):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
        return path

    def on_path(self, *dirs):
        return patch.dict(os.environ, {"PATH": os.pathsep.join(str(d) for d in dirs)})

    def wrapper_row(self, *dirs, settings=None):
        with self.on_path(*dirs):
            return doctor.check_wrapper(self.settings if settings is None else settings, self.accounts)

    def test_standalone_install_ahead_of_wrapped_entry_fails_and_says_it_self_heals(self):
        (self.bin_a / "codex").symlink_to(self.standalone)
        row = self.wrapper_row(self.bin_a, self.bin_b)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn(f"plain codex runs {self.bin_a / 'codex'} -> {self.standalone}, not xswap-codex", row["detail"])
        self.assertIn(f"shadows the wrapped entry {self.bin_b / 'codex'}", row["detail"])
        self.assertIn("next xswap launch, list, or use wraps it", row["detail"])
        self.assertIn(self.reconnect, row["detail"])

    def test_shadowing_regular_file_fails_and_cannot_be_wrapped(self):
        self.executable("bin-a/codex")
        row = self.wrapper_row(self.bin_a, self.bin_b)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn(f"plain codex runs {self.bin_a / 'codex'}, not xswap-codex", row["detail"])
        self.assertIn("cannot wrap it (not-a-symlink)", row["detail"])
        self.assertIn("is not a symlink, so xswap leaves it alone", row["detail"])
        self.assertIn("Fix: replace it with a user-owned symlink", row["detail"])
        self.assertNotIn("next xswap launch", row["detail"])

    def test_shadowing_entry_when_wrapped_entry_is_off_path_names_the_manual_fix(self):
        (self.bin_a / "codex").symlink_to(self.standalone)
        row = self.wrapper_row(self.bin_a)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn(f"the wrapped entry {self.bin_b / 'codex'} is not on this shell's PATH", row["detail"])
        self.assertIn(self.reconnect, row["detail"])
        self.assertNotIn("next xswap launch", row["detail"])

    def test_wrapped_first_with_foreign_entry_later_warns_shadowed(self):
        (self.bin_a / "codex").symlink_to(self.standalone)
        row = self.wrapper_row(self.bin_b, self.bin_a)
        self.assertEqual(row["status"], "WARN")
        self.assertTrue(row["detail"].startswith(f"{self.bin_b / 'codex'} -> xswap-codex"))
        self.assertIn("shadowed on PATH", row["detail"])
        self.assertIn("full path", row["detail"])
        self.assertIn(f"{self.bin_a / 'codex'} -> {self.standalone}", row["detail"])

    def test_every_entry_wrapped_is_ok_with_count(self):
        (self.bin_a / "codex").symlink_to(self.proxy)
        row = self.wrapper_row(self.bin_a, self.bin_b)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["detail"], f"{self.bin_a / 'codex'} -> xswap-codex; 2 codex entries on PATH, all wrapped")

    def test_single_wrapped_entry_is_ok(self):
        row = self.wrapper_row(self.bin_b)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["detail"], f"{self.bin_b / 'codex'} -> xswap-codex")

    def test_duplicate_path_directories_count_once(self):
        row = self.wrapper_row(self.bin_b, self.bin_b)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["detail"], f"{self.bin_b / 'codex'} -> xswap-codex")

    def test_relative_link_to_the_proxy_counts_as_wrapped(self):
        (self.bin_a / "codex").symlink_to(os.path.relpath(self.proxy, self.bin_a))
        row = self.wrapper_row(self.bin_a, self.bin_b)
        self.assertEqual(row["status"], "OK")
        self.assertIn("2 codex entries on PATH, all wrapped", row["detail"])

    def test_dangling_and_non_executable_entries_are_skipped(self):
        (self.bin_a / "codex").symlink_to(self.base / "missing")
        notes = self.empty / "codex"
        notes.write_text("notes")
        notes.chmod(0o600)
        row = self.wrapper_row(self.bin_a, self.empty, self.bin_b)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["detail"], f"{self.bin_b / 'codex'} -> xswap-codex")

    def test_recorded_entry_drifted_fails_and_says_it_reconnects(self):
        link = self.bin_b / "codex"
        link.unlink()
        link.symlink_to(self.standalone)
        row = self.wrapper_row(self.bin_b)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("changed outside xswap (a Codex update replaces the link)", row["detail"])
        self.assertIn(f"{link} -> {self.standalone}", row["detail"])
        self.assertIn("next xswap launch, list, or use reconnects it", row["detail"])
        self.assertIn(self.reconnect, row["detail"])

    def test_recorded_entry_replaced_by_regular_file_cannot_reconnect(self):
        (self.bin_b / "codex").unlink()
        self.executable("bin-b/codex")
        row = self.wrapper_row(self.bin_b)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("changed outside xswap (not-a-symlink)", row["detail"])
        self.assertIn("Fix: replace it with a user-owned symlink", row["detail"])

    def test_wrapped_entry_off_path_warns(self):
        row = self.wrapper_row(self.empty)
        self.assertEqual(row["status"], "WARN")
        self.assertIn(f"{self.bin_b / 'codex'} -> xswap-codex, but {self.bin_b} is not on this shell's PATH", row["detail"])

    def test_no_codex_anywhere_and_dangling_record_fails(self):
        link = self.bin_b / "codex"
        link.unlink()
        link.symlink_to(self.base / "missing")
        row = self.wrapper_row(self.bin_b)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("no longer runs", row["detail"])
        self.assertIn("reinstall Codex", row["detail"])
        self.assertIn(self.reconnect, row["detail"])

    def test_disabled_auto_reports_not_connected_instead_of_drift(self):
        # After auto-disable the entry is restored; the dormant record used to read as "changed outside xswap".
        link = self.bin_b / "codex"
        link.unlink()
        link.symlink_to(self.brew)
        settings = {**self.settings, "enabled": False}
        row = self.wrapper_row(self.bin_b, settings=settings)
        self.assertEqual(row["status"], "WARN")
        self.assertIn("codex command not connected", row["detail"])
        self.assertIn(self.reconnect, row["detail"])
        with self.on_path(self.bin_b):
            self.assertEqual(doctor.check_wrapper(settings)["status"], "OK")

    def test_doctor_exit_code_and_json_through_main(self):
        (self.bin_a / "codex").symlink_to(self.standalone)
        source = self.base / "source"
        source.mkdir()
        source.joinpath("config.toml").write_text('model = "example"\n')
        atomic_json(source / "auth.json", {"auth_mode": "chatgpt", "tokens": {
            "access_token": fake_jwt(exp=time.time() + 100000), "refresh_token": "fixture-refresh"}})
        manager = Manager(self.base / "store", source)
        manager.register("main")
        atomic_json(manager.root / "auto.json", {**self.settings, "accounts": ["main"]})
        env = {"CODEX_SWAP_HOME": str(manager.root), "CODEX_HOME": str(source),
               "OPENCLAW_STATE_DIR": str(self.base / "no-openclaw-here")}
        with patch.dict(os.environ, env), self.on_path(self.bin_a, self.bin_b), \
                patch("shutil.which", side_effect=lambda n: str(self.bin_a / "codex") if n == "codex" else None), \
                patch("subprocess.run", return_value=type("R", (), {"returncode": 0, "stdout": "0.1.0", "stderr": ""})()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["doctor", "--json"])
        self.assertEqual(code, 1)
        rows = json.loads(out.getvalue())
        row = find(rows, "wrapper")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn(str(self.bin_a / "codex"), row["detail"])
        self.assertEqual([r["name"] for r in rows if r["status"] == "FAIL"], ["wrapper"])
        self.assertNotIn("fixture-refresh", out.getvalue())


def expected_profile_id(account_id, subject):
    """Independent re-implementation of xswap_openclaw_state.profile_id_for_home's
    hash so a test comparing against it isn't just checking the module against itself."""
    digest = hashlib.sha256(f"{account_id}\0{subject}".encode()).hexdigest()[:24]
    return f"openai:xswap-{digest}"


class OpenclawCooldownDoctorTests(unittest.TestCase):
    """`xswap doctor`'s "NAME: openclaw cooldown" check, isolated to a fixture sqlite
    database that mirrors OpenClaw's real config_machine_state DDL (verified read-only
    against a real ~/.openclaw/state/openclaw.sqlite while writing this test)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.manager = Manager(self.base / "store", self.source)
        self.state_dir = self.base / "openclaw-state"
        self.enterContext(patch.dict(os.environ, {"OPENCLAW_STATE_DIR": str(self.state_dir)}))

    def register_openclaw_account(self, name, account_id, subject, disabled=False, exp=None):
        home = self.base / f"home-{name}"
        home.mkdir()
        token = fake_jwt(exp=exp if exp is not None else time.time() + 100000,
                          claims={"sub": subject, "https://api.openai.com/auth": {"chatgpt_account_id": account_id}})
        atomic_json(home / "auth.json", {"auth_mode": "chatgpt", "tokens": {"access_token": token, "refresh_token": "fixture-refresh"}})
        self.manager.register(name, home)
        if disabled:
            self.manager.set_disabled(name, True)
        return home

    def write_state_db(self, usage_stats, mode=0o600):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "openclaw.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("""CREATE TABLE config_machine_state (
              state_key TEXT NOT NULL PRIMARY KEY,
              value_json TEXT NOT NULL,
              updated_at_ms INTEGER NOT NULL
            ) STRICT""")
        state = {"version": 1, "usageStats": usage_stats}
        conn.execute("INSERT INTO config_machine_state (state_key, value_json, updated_at_ms) VALUES (?, ?, ?)",
                     ("authProfiles.state", json.dumps(state), 0))
        conn.commit()
        conn.close()
        os.chmod(path, mode)
        return path

    def run_doctor(self):
        def run(cmd, **kwargs):
            class Result:
                returncode = 0
                stdout = "1.2.3"
                stderr = ""
            return Result()

        with patch("shutil.which", side_effect=lambda n: "/fixture/codex" if n == "codex" else None), \
                patch("subprocess.run", side_effect=run):
            return doctor.run(self.manager)

    def test_db_missing_reports_no_row(self):
        self.register_openclaw_account("main", "acct-1", "sub-1")
        results = self.run_doctor()
        self.assertFalse(any(r["name"] == "main: openclaw cooldown" for r in results))

    def test_no_cooldown_is_ok(self):
        self.register_openclaw_account("main", "acct-1", "sub-1")
        self.write_state_db({})
        row = find(self.run_doctor(), "main: openclaw cooldown")
        self.assertEqual(row["status"], "OK")

    def test_cooldown_with_remaining_quota_fails(self):
        profile_id = expected_profile_id("acct-1", "sub-1")
        self.register_openclaw_account("main", "acct-1", "sub-1")
        self.write_state_db({profile_id: {
            "blockedUntil": (time.time() + 6 * 86400) * 1000,
            "blockedReason": "subscription_limit", "blockedSource": "wham", "errorCount": 1}})
        buckets = [{"id": "codex", "name": "Codex", "reached": None, "credits": None, "windows": [
            {"position": "secondary", "usedPercent": 0, "remainingPercent": 100, "windowMinutes": 10080, "resetsAt": None}]}]
        self.manager.remember_usage("main", buckets, time.time(), "ChatGPT")
        row = find(self.run_doctor(), "main: openclaw cooldown")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("xswap openclaw --clear-cooldown main", row["detail"])

    def test_cooldown_with_unknown_remaining_warns(self):
        profile_id = expected_profile_id("acct-1", "sub-1")
        self.register_openclaw_account("main", "acct-1", "sub-1")
        self.write_state_db({profile_id: {
            "blockedUntil": (time.time() + 6 * 86400) * 1000,
            "blockedReason": "subscription_limit", "blockedSource": "wham", "errorCount": 1}})
        # No usage-cache.json entry at all: doctor must not spawn a process to find out.
        row = find(self.run_doctor(), "main: openclaw cooldown")
        self.assertEqual(row["status"], "WARN")
        self.assertIn("xswap list", row["detail"])

    def test_expired_cooldown_is_not_reported_as_blocked(self):
        profile_id = expected_profile_id("acct-1", "sub-1")
        self.register_openclaw_account("main", "acct-1", "sub-1")
        self.write_state_db({profile_id: {
            "blockedUntil": (time.time() - 3600) * 1000,
            "blockedReason": "subscription_limit", "blockedSource": "wham", "errorCount": 1}})
        row = find(self.run_doctor(), "main: openclaw cooldown")
        self.assertEqual(row["status"], "OK")

    def test_disabled_account_in_cooldown_never_fails(self):
        profile_id = expected_profile_id("acct-1", "sub-1")
        self.register_openclaw_account("main", "acct-1", "sub-1", disabled=True)
        self.write_state_db({profile_id: {
            "blockedUntil": (time.time() + 6 * 86400) * 1000,
            "blockedReason": "subscription_limit", "blockedSource": "wham", "errorCount": 1}})
        buckets = [{"id": "codex", "name": "Codex", "reached": None, "credits": None, "windows": [
            {"position": "secondary", "usedPercent": 0, "remainingPercent": 100, "windowMinutes": 10080, "resetsAt": None}]}]
        self.manager.remember_usage("main", buckets, time.time(), "ChatGPT")
        results = self.run_doctor()
        row = find(results, "main: openclaw cooldown")
        self.assertEqual(row["status"], "WARN")
        self.assertIn("disabled", row["detail"])
        self.assertFalse(any(r["status"] == "FAIL" for r in results))

    def test_api_key_account_has_no_row(self):
        home = self.base / "home-keyed"
        home.mkdir()
        atomic_json(home / "auth.json", {"auth_mode": "apikey", "OPENAI_API_KEY": "fixture-key"})
        self.manager.register("keyed", home)
        self.write_state_db({})
        results = self.run_doctor()
        self.assertFalse(any(r["name"] == "keyed: openclaw cooldown" for r in results))

    def test_other_profiles_and_accounts_are_independent(self):
        profile_a = expected_profile_id("acct-a", "sub-a")
        self.register_openclaw_account("main", "acct-a", "sub-a")
        self.register_openclaw_account("second", "acct-b", "sub-b")
        self.write_state_db({profile_a: {
            "blockedUntil": (time.time() + 3600) * 1000,
            "blockedReason": "subscription_limit", "blockedSource": "wham", "errorCount": 1}})
        results = self.run_doctor()
        self.assertEqual(find(results, "main: openclaw cooldown")["status"], "WARN")
        self.assertEqual(find(results, "second: openclaw cooldown")["status"], "OK")


if __name__ == "__main__":
    unittest.main()
