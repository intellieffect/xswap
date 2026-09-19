"""Golden snapshot tests for the versioned `--json` payload contract (INT-5611).

Each payload builder in `xswap.json_output` is exercised against deterministic fixture
data (fixed timestamps, two synthetic accounts, no network) and compared byte-for-byte
against a checked-in JSON file under tests/snapshots/json/. A snapshot mismatch means the
shape of a payload changed -- an added/removed/renamed key or a type change.

If the change is additive (a new field): update the snapshot.
If the change is breaking (a field renamed, removed, or its type/meaning changed): bump
`xswap.json_output.SCHEMA_VERSION` first, then update the snapshot.

Regenerate every snapshot from current output with:

    XSWAP_UPDATE_SNAPSHOTS=1 python -m unittest test_json_contract

Snapshot files must never contain a real account label, email, or filesystem path --
only the synthetic fixture values defined in this file.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from xswap import json_output as jo

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "json"
UPDATE_ENV_VAR = "XSWAP_UPDATE_SNAPSHOTS"

FIXED_TIME = 1_700_000_000.0

FIXTURE_ROWS = [
    {
        "name": "work", "identity": "work@example.test", "status": jo.STATUS_OK,
        "buckets": [{"id": "codex", "usedPercent": 12.5, "windowMinutes": 300, "reached": None}],
        "resetCredits": None, "fetchedAt": FIXED_TIME, "disabled": False, "cached": False,
        "slot": 1, "active": True,
    },
    {
        "name": "spare", "identity": "spare@example.test", "status": jo.STATUS_AUTH_FAILED,
        "buckets": [], "resetCredits": None, "fetchedAt": None, "disabled": False, "cached": False,
        "slot": 2, "active": False,
    },
]

FIXTURE_DOCTOR_RESULTS = [
    {"name": "codex binary", "status": "OK", "detail": "/usr/local/bin/codex"},
    {"name": "accounts", "status": "WARN", "detail": "no accounts registered; run: xswap register main"},
]

FIXTURE_AUTO_STATUS_STATE = {
    "enabled": True, "accounts": ["work", "spare"], "codexWrapped": True,
    "wrapperReason": "connected", "weeklyRemainingThreshold": 10,
    "pruned": 0, "prunedRuns": [], "sessions": [],
}

FIXTURE_AUTO_TICK_PAYLOAD = {
    "decision": "switched", "reason": "switched", "exitCode": 0, "selected": "work",
    "target": "spare", "reserve": 10, "dryRun": False,
    "remaining": {"work": 5, "spare": 80}, "candidates": [{"name": "spare", "remaining7d": 80}],
    "report": None, "lines": ["switched: work -> spare (work weekly 5% left)"],
}

FIXTURE_DASHBOARD_DATA = {
    "accounts": [{"name": "work", "active": True, "line": "work 12.5% used"}],
    "policy": "auto: weekly reserve 10%",
    "sessions": [{"account": "work", "surface": "cli"}],
    "updatedAt": FIXED_TIME,
}


class JsonContractSnapshotTests(unittest.TestCase):
    """One case per builder in xswap.json_output, each pinned to a snapshot file."""

    def _assert_matches_snapshot(self, name, payload):
        path = SNAPSHOT_DIR / f"{name}.json"
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        if os.environ.get(UPDATE_ENV_VAR):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered)
            return
        self.assertTrue(path.exists(), f"missing snapshot {path}; regenerate with {UPDATE_ENV_VAR}=1")
        expected = path.read_text()
        self.assertEqual(
            rendered, expected,
            f"{name} payload shape changed.\n"
            "If this is additive, regenerate with XSWAP_UPDATE_SNAPSHOTS=1.\n"
            "If this is breaking (a field renamed/removed/retyped), bump "
            "xswap.json_output.SCHEMA_VERSION first, then regenerate.",
        )

    def test_accounts_payload_list_v1(self):
        self._assert_matches_snapshot("list", jo.accounts_payload(FIXTURE_ROWS))

    def test_usage_payload_is_the_same_builder_as_list(self):
        # `usage --json` renders one row through the identical accounts_payload builder.
        self._assert_matches_snapshot("usage", jo.accounts_payload(FIXTURE_ROWS[:1]))

    def test_doctor_payload_list_v1(self):
        self._assert_matches_snapshot("doctor", jo.doctor_payload(FIXTURE_DOCTOR_RESULTS))

    def test_auto_status_payload_v1(self):
        self._assert_matches_snapshot("auto-status", jo.auto_status_payload(FIXTURE_AUTO_STATUS_STATE))

    def test_auto_tick_payload_v1(self):
        self._assert_matches_snapshot("auto-tick", jo.auto_tick_payload(FIXTURE_AUTO_TICK_PAYLOAD))

    def test_dashboard_payload_v1(self):
        self._assert_matches_snapshot("dashboard", jo.dashboard_payload(FIXTURE_DASHBOARD_DATA))

    def test_object_payloads_carry_schema_version_first(self):
        for payload in (
            jo.auto_status_payload(FIXTURE_AUTO_STATUS_STATE),
            jo.auto_tick_payload(FIXTURE_AUTO_TICK_PAYLOAD),
            jo.dashboard_payload(FIXTURE_DASHBOARD_DATA),
        ):
            self.assertEqual(next(iter(payload)), "schemaVersion")
            self.assertEqual(payload["schemaVersion"], jo.SCHEMA_VERSION)

    def test_list_payloads_stay_bare_lists(self):
        self.assertIsInstance(jo.accounts_payload(FIXTURE_ROWS), list)
        self.assertIsInstance(jo.doctor_payload(FIXTURE_DOCTOR_RESULTS), list)

    def test_no_snapshot_contains_a_real_account_label_or_path(self):
        for path in SNAPSHOT_DIR.glob("*.json"):
            text = path.read_text()
            for marker in ("bigno", "intellieffect", "@intellieffect", "/Users/", "/Volumes/"):
                self.assertNotIn(marker, text, f"{path} leaks {marker!r}")


if __name__ == "__main__":
    unittest.main()
