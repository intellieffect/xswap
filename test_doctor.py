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

    def run_doctor(self, codex="/fixture/codex", codex_version="1.2.3"):
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

        with patch("shutil.which", side_effect=which), patch("subprocess.run", side_effect=run):
            return doctor.run(self.manager)

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

    # --- check_wrapper ---

    def test_check_wrapper_not_connected_is_ok(self):
        row = doctor.check_wrapper({})
        self.assertEqual(row["status"], "OK")
        self.assertEqual(doctor.check_wrapper({}, {"main": {}})["status"], "OK")
        # A disabled account does not count toward the two needed for switching.
        self.assertEqual(doctor.check_wrapper({}, {"main": {}, "old": {"disabled": True}})["status"], "OK")

    def test_check_wrapper_not_connected_with_two_accounts_warns(self):
        row = doctor.check_wrapper({}, {"main": {}, "work": {}})
        self.assertEqual(row["status"], "WARN")
        self.assertIn("xswap auto-enable --accounts main,work --wrap-codex", row["detail"])

    def test_check_wrapper_matches_proxy_is_ok(self):
        link = self.base / "codex-link"
        link.symlink_to("/fixture/xswap-codex")
        settings = {"wrapper": {"path": str(link), "proxy": "/fixture/xswap-codex"}}
        row = doctor.check_wrapper(settings)
        self.assertEqual(row["status"], "OK")

    def test_check_wrapper_changed_externally_warns(self):
        link = self.base / "codex-link-2"
        link.symlink_to("/something-else")
        settings = {"wrapper": {"path": str(link), "proxy": "/fixture/xswap-codex"}}
        row = doctor.check_wrapper(settings)
        self.assertEqual(row["status"], "WARN")

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
