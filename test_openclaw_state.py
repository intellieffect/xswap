"""Unit tests for xswap_openclaw_state: all against temp sqlite copies, never the real
~/.openclaw/state/openclaw.sqlite. The config_machine_state DDL used in write_db() was
verified read-only against a real installation's database while writing these tests."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_swap import atomic_json
import xswap_openclaw_state as state


def jwt(claims):
    header = base64.urlsafe_b64encode(b"{}").rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return (header + b"." + payload + b".fixture-sig").decode()


class SqliteFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def write_db(self, usage_stats=None, extra_state=None, mode=0o600, path=None):
        path = path or (self.base / "openclaw.sqlite")
        conn = sqlite3.connect(str(path))
        conn.execute("""CREATE TABLE config_machine_state (
              state_key TEXT NOT NULL PRIMARY KEY,
              value_json TEXT NOT NULL,
              updated_at_ms INTEGER NOT NULL
            ) STRICT""")
        value = dict(extra_state or {})
        value["version"] = 1
        if usage_stats is not None:
            value["usageStats"] = usage_stats
        conn.execute("INSERT INTO config_machine_state (state_key, value_json, updated_at_ms) VALUES (?, ?, ?)",
                     (state.STATE_KEY, json.dumps(value), 0))
        conn.commit()
        conn.close()
        os.chmod(path, mode)
        return path

    def read_raw_state(self, path):
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("SELECT value_json FROM config_machine_state WHERE state_key = ?", (state.STATE_KEY,)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0])


class ReadCooldownsTests(SqliteFixture):
    def test_missing_database_returns_empty(self):
        self.assertEqual(state.read_cooldowns(self.base / "absent.sqlite"), {})

    def test_no_usage_stats_returns_empty(self):
        path = self.write_db(usage_stats=None)
        self.assertEqual(state.read_cooldowns(path), {})

    def test_future_cooldown_is_reported(self):
        now_ms = time.time() * 1000
        path = self.write_db({"openai:xswap-aaa": {
            "blockedUntil": now_ms + 6 * 86400 * 1000, "blockedReason": "subscription_limit",
            "blockedSource": "wham", "errorCount": 1}})
        result = state.read_cooldowns(path, now=now_ms)
        self.assertEqual(set(result), {"openai:xswap-aaa"})
        entry = result["openai:xswap-aaa"]
        self.assertEqual(entry["blockedReason"], "subscription_limit")
        self.assertEqual(entry["blockedSource"], "wham")
        self.assertEqual(entry["errorCount"], 1)

    def test_expired_cooldown_is_not_reported(self):
        now_ms = time.time() * 1000
        path = self.write_db({"openai:xswap-aaa": {"blockedUntil": now_ms - 1000, "blockedReason": "subscription_limit"}})
        self.assertEqual(state.read_cooldowns(path, now=now_ms), {})

    def test_entry_without_blocked_until_is_ignored(self):
        path = self.write_db({"openai:xswap-aaa": {"errorCount": 3}})
        self.assertEqual(state.read_cooldowns(path), {})

    def test_multiple_profiles_are_independent(self):
        now_ms = time.time() * 1000
        path = self.write_db({
            "openai:xswap-aaa": {"blockedUntil": now_ms + 60000, "blockedReason": "rate_limit"},
            "openai:xswap-bbb": {"blockedUntil": now_ms - 60000, "blockedReason": "rate_limit"},
        })
        result = state.read_cooldowns(path, now=now_ms)
        self.assertEqual(set(result), {"openai:xswap-aaa"})

    def test_symlinked_database_is_refused(self):
        real = self.write_db({})
        link = self.base / "link.sqlite"
        link.symlink_to(real)
        with self.assertRaises(state.OpenClawStateError):
            state.read_cooldowns(link)

    def test_world_readable_database_is_refused(self):
        path = self.write_db({}, mode=0o644)
        with self.assertRaises(state.OpenClawStateError):
            state.read_cooldowns(path)

    def test_missing_table_returns_empty(self):
        path = self.base / "no-table.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        os.chmod(path, 0o600)
        self.assertEqual(state.read_cooldowns(path), {})

    def test_invalid_json_raises(self):
        path = self.base / "bad.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("""CREATE TABLE config_machine_state (
              state_key TEXT NOT NULL PRIMARY KEY, value_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL) STRICT""")
        conn.execute("INSERT INTO config_machine_state VALUES (?, ?, ?)", (state.STATE_KEY, "not json", 0))
        conn.commit()
        conn.close()
        os.chmod(path, 0o600)
        with self.assertRaises(state.OpenClawStateError):
            state.read_cooldowns(path)


class ClearCooldownTests(SqliteFixture):
    def test_clears_exactly_the_three_keys_and_zeroes_error_count(self):
        now_ms = time.time() * 1000
        path = self.write_db({
            "openai:xswap-target": {"blockedUntil": now_ms + 60000, "blockedReason": "subscription_limit",
                                     "blockedSource": "wham", "errorCount": 3, "lastUsed": 111},
            "openai:xswap-other": {"blockedUntil": now_ms + 60000, "blockedReason": "rate_limit",
                                    "blockedSource": "wham", "errorCount": 2},
        }, extra_state={"order": {"openai": ["openai:xswap-target", "openai:xswap-other"]},
                         "lastGood": {"openai": "openai:xswap-target"}})
        backup_dir = self.base / "backups"
        backup_path = state.clear_cooldown(path, ["openai:xswap-target"], backup_dir)

        self.assertTrue(backup_path.exists())
        self.assertEqual(os.stat(backup_path).st_mode & 0o777, 0o600)
        # The backup is a full untouched copy taken before the write.
        backup_conn = sqlite3.connect(str(backup_path))
        try:
            backup_row = backup_conn.execute("SELECT value_json FROM config_machine_state WHERE state_key = ?", (state.STATE_KEY,)).fetchone()
        finally:
            backup_conn.close()
        backup_state = json.loads(backup_row[0])
        self.assertIn("blockedUntil", backup_state["usageStats"]["openai:xswap-target"])

        updated = self.read_raw_state(path)
        target = updated["usageStats"]["openai:xswap-target"]
        self.assertNotIn("blockedUntil", target)
        self.assertNotIn("blockedReason", target)
        self.assertNotIn("blockedSource", target)
        self.assertEqual(target["errorCount"], 0)
        self.assertEqual(target["lastUsed"], 111)  # unrelated key untouched

        other = updated["usageStats"]["openai:xswap-other"]
        self.assertEqual(other["blockedUntil"], now_ms + 60000)
        self.assertEqual(other["blockedReason"], "rate_limit")
        self.assertEqual(other["errorCount"], 2)

        # order/lastGood are untouched by a cooldown clear.
        self.assertEqual(updated["order"], {"openai": ["openai:xswap-target", "openai:xswap-other"]})
        self.assertEqual(updated["lastGood"], {"openai": "openai:xswap-target"})

    def test_missing_database_raises(self):
        with self.assertRaises(state.OpenClawStateError):
            state.clear_cooldown(self.base / "absent.sqlite", ["openai:xswap-a"], self.base / "backups")

    def test_symlinked_database_is_refused_and_nothing_is_backed_up(self):
        real = self.write_db({"openai:xswap-a": {"blockedUntil": time.time() * 1000 + 60000}})
        link = self.base / "link.sqlite"
        link.symlink_to(real)
        backup_dir = self.base / "backups"
        with self.assertRaises(state.OpenClawStateError):
            state.clear_cooldown(link, ["openai:xswap-a"], backup_dir)
        self.assertFalse(backup_dir.exists())

    def test_no_profile_ids_raises(self):
        path = self.write_db({})
        with self.assertRaises(state.OpenClawStateError):
            state.clear_cooldown(path, [], self.base / "backups")

    def test_unknown_profile_id_is_a_no_op_but_still_backs_up(self):
        path = self.write_db({"openai:xswap-real": {"blockedUntil": time.time() * 1000 + 60000, "errorCount": 1}})
        backup_path = state.clear_cooldown(path, ["openai:xswap-ghost"], self.base / "backups")
        self.assertTrue(backup_path.exists())
        updated = self.read_raw_state(path)
        self.assertEqual(updated["usageStats"]["openai:xswap-real"]["errorCount"], 1)

    def test_missing_state_row_still_backs_up_without_error(self):
        path = self.base / "empty.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("""CREATE TABLE config_machine_state (
              state_key TEXT NOT NULL PRIMARY KEY, value_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL) STRICT""")
        conn.commit()
        conn.close()
        os.chmod(path, 0o600)
        backup_path = state.clear_cooldown(path, ["openai:xswap-a"], self.base / "backups")
        self.assertTrue(backup_path.exists())


class ProfileIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()

    def write_auth(self, extra=None, tokens_extra=None):
        tokens = {"access_token": jwt({"sub": "sub-fixture", "https://api.openai.com/auth": {"chatgpt_account_id": "acct-fixture"}}),
                  "refresh_token": "fixture-refresh"}
        if tokens_extra:
            tokens.update(tokens_extra)
        data = {"auth_mode": "chatgpt", "tokens": tokens}
        if extra:
            data.update(extra)
        atomic_json(self.home / "auth.json", data)

    def test_matches_the_bridge_digest_formula(self):
        self.write_auth()
        expected_digest = hashlib.sha256(b"acct-fixture\0sub-fixture").hexdigest()[:24]
        self.assertEqual(state.profile_id_for_home(self.home), f"openai:xswap-{expected_digest}")

    def test_tokens_account_id_takes_priority_over_the_auth_claim(self):
        self.write_auth(tokens_extra={"account_id": "explicit-account"})
        expected_digest = hashlib.sha256(b"explicit-account\0sub-fixture").hexdigest()[:24]
        self.assertEqual(state.profile_id_for_home(self.home), f"openai:xswap-{expected_digest}")

    def test_api_key_account_has_no_profile_id(self):
        atomic_json(self.home / "auth.json", {"auth_mode": "apikey", "OPENAI_API_KEY": "fixture-key"})
        self.assertIsNone(state.profile_id_for_home(self.home))

    def test_missing_subject_claim_has_no_profile_id(self):
        token = jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-fixture"}})
        atomic_json(self.home / "auth.json", {"auth_mode": "chatgpt", "tokens": {"access_token": token, "refresh_token": "r"}})
        self.assertIsNone(state.profile_id_for_home(self.home))

    def test_no_auth_json_has_no_profile_id(self):
        self.assertIsNone(state.profile_id_for_home(self.home))

    def test_unsafe_auth_json_permissions_have_no_profile_id(self):
        self.write_auth()
        (self.home / "auth.json").chmod(0o644)
        self.assertIsNone(state.profile_id_for_home(self.home))


class DefaultSqlitePathTests(unittest.TestCase):
    def test_env_override_wins(self):
        with patch.dict(os.environ, {"OPENCLAW_STATE_DIR": "/tmp/fixture-openclaw-state"}):
            self.assertEqual(state.default_sqlite_path(), (Path("/tmp/fixture-openclaw-state") / "openclaw.sqlite").resolve())

    def test_default_is_under_home(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENCLAW_STATE_DIR", None)
            self.assertEqual(state.default_sqlite_path(), Path.home() / ".openclaw" / "state" / "openclaw.sqlite")


if __name__ == "__main__":
    unittest.main()
