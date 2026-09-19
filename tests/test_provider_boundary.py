"""The provider boundary, exercised from the neutral side (INT-5614).

Every test here drives `xswap.core` with `FakeProvider` -- a platform whose
quota bucket is called "widget" and whose windows are 60 and 4320 minutes. If
any of core still assumed Codex's bucket id or its 300/10080 windows, these
would fail while the rest of the suite (which only ever sees Codex) stayed
green. The last class checks the other direction: that the real Codex provider
is reachable through the registry and still declares every surface it has.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes.provider import FAKE_QUOTA, FakeProvider, bucket, row

from xswap import providers
from xswap.core import tick
from xswap.core.doctor import collect, format_report
from xswap.core.ranking import rank_candidates
from xswap.core.types import Check, CredentialState, UsageSnapshot


class RankingOverASecondPlatform(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()

    def test_the_fullest_account_wins_on_the_fake_platform_s_own_windows(self):
        rows = [row("low", short=10, weekly=10), row("high", short=90, weekly=90)]
        name, detail = rank_candidates(rows, shape=self.provider.quota_shape)
        self.assertEqual(name, "high")
        self.assertEqual({c["name"]: c["remaining7d"] for c in detail["candidates"]},
                         {"low": 10, "high": 90})

    def test_a_reserve_is_read_against_the_fake_platform_s_weekly_window(self):
        rows = [row("thin", short=90, weekly=5), row("thick", short=90, weekly=40)]
        self.assertEqual(rank_candidates(rows, weekly_remaining=20, shape=self.provider.quota_shape)[0], "thick")
        self.assertIsNone(rank_candidates(rows, weekly_remaining=50, shape=self.provider.quota_shape)[0])

    def test_disabled_and_excluded_rows_are_never_ranked(self):
        rows = [row("a", weekly=90, disabled=True), row("b", weekly=80), row("c", weekly=70)]
        self.assertEqual(rank_candidates(rows, exclude=("b",), shape=self.provider.quota_shape)[0], "c")

    def test_usage_snapshots_rank_like_the_rows_they_came_from(self):
        """`read_usage` answers in `UsageSnapshot`s; ranking has to take them as they are."""
        snapshots = []
        for name, weekly in (("low", 10), ("high", 90)):
            snapshot = UsageSnapshot(account=name, buckets=[bucket(90, weekly)], shape=FAKE_QUOTA)
            self.assertIsInstance(snapshot, UsageSnapshot)
            snapshots.append(snapshot)
        self.assertEqual(rank_candidates(snapshots, shape=FAKE_QUOTA)[0], "high")

    def test_an_unknown_window_is_never_treated_as_headroom(self):
        rows = [row("known", weekly=80), row("unknown", weekly=None)]
        self.assertEqual(rank_candidates(rows, weekly_remaining=10, shape=FAKE_QUOTA)[0], "known")


class TickDecidesOnASecondPlatform(unittest.TestCase):
    SHAPE = FAKE_QUOTA

    def decide(self, rows, current, pool, reserve, failed=frozenset()):
        return tick.decide(rows, current, pool, reserve, failed, self.SHAPE)

    def test_no_action_while_the_selected_account_is_above_the_reserve(self):
        rows = [row("a", weekly=60, active=True), row("b", weekly=90)]
        outcome = self.decide(rows, "a", ["a", "b"], 20)
        self.assertEqual((outcome["decision"], outcome["reason"]), ("no-action", "above-reserve"))
        self.assertIsNone(outcome["target"])

    def test_a_spent_weekly_window_switches_to_the_best_pool_account(self):
        rows = [row("a", weekly=5, active=True), row("b", weekly=90)]
        outcome = self.decide(rows, "a", ["a", "b"], 20)
        self.assertEqual(outcome["decision"], "switch")
        self.assertEqual(outcome["target"], "b")
        self.assertIn("at or below the 20% reserve", outcome["message"])

    def test_the_fake_platform_s_bucket_names_itself_in_the_shortfall_line(self):
        rows = [row("a", short=0, weekly=90, active=True), row("b", weekly=90)]
        outcome = self.decide(rows, "a", ["a", "b"], 0)
        self.assertEqual(outcome["decision"], "switch")
        self.assertIn("exhausted widget window", outcome["message"])

    def test_no_candidate_above_the_reserve_blocks_instead_of_switching(self):
        rows = [row("a", weekly=5, active=True), row("b", weekly=8)]
        outcome = self.decide(rows, "a", ["a", "b"], 20)
        self.assertEqual((outcome["decision"], outcome["reason"]), ("blocked", "no-candidate"))

    def test_unknown_quota_never_switches(self):
        rows = [row("a", weekly=None, active=True), row("b", weekly=90)]
        outcome = self.decide(rows, "a", ["a", "b"], 20)
        self.assertEqual((outcome["decision"], outcome["reason"]), ("no-action", "quota-unknown"))

    def test_an_auth_failed_selection_switches_even_without_quota(self):
        rows = [row("a", weekly=None, active=True), row("b", weekly=90)]
        outcome = self.decide(rows, "a", ["a", "b"], 20, failed=frozenset({"a"}))
        self.assertEqual(outcome["decision"], "switch")
        self.assertEqual(outcome["target"], "b")


class RegistryRecordsItsProvider(unittest.TestCase):
    """`accounts.json` names the platform each account belongs to, without a migration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True)
        (self.home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "x", "id_token": "y"}}))
        (self.home / "auth.json").chmod(0o600)

    def manager(self):
        from xswap.manager import Manager
        return Manager(root=self.root, source=self.home)

    def test_a_new_account_records_the_provider_that_created_it(self):
        manager = self.manager()
        with patch("xswap.manager.identity", return_value="user@example.test"), \
             patch("xswap.manager.check_file_store"):
            manager.register("main")
        entry = json.loads((self.root / "accounts.json").read_text())["accounts"]["main"]
        self.assertEqual(entry["provider"], "codex")

    def test_a_record_without_the_field_reads_as_this_build_s_provider(self):
        manager = self.manager()
        entry = {"home": str(self.home), "managed": False}
        self.assertEqual(manager._registry.provider_of(entry), manager.provider.name)

    def test_an_explicit_provider_in_a_record_wins_over_the_default(self):
        manager = self.manager()
        self.assertEqual(manager._registry.provider_of({"home": "/x", "provider": "widget"}), "widget")

    def test_reading_a_pre_0_8_3_registry_never_rewrites_it(self):
        """A read-only command must not migrate `accounts.json` behind the user's back."""
        self.root.mkdir(parents=True)
        original = json.dumps({"version": 1, "active": "main",
                               "accounts": {"main": {"home": str(self.home), "managed": False}}})
        path = self.root / "accounts.json"
        path.write_text(original)
        manager = self.manager()
        for _ in range(3):
            manager.read()
            manager.account("main")
            manager.enabled_accounts()
        self.assertEqual(path.read_text(), original)


class DoctorRunsASecondPlatformSChecks(unittest.TestCase):
    def test_the_framework_prints_whatever_the_provider_reports(self):
        class Fake:
            provider = FakeProvider(checks=[("widget binary", "OK", "/usr/bin/widget"),
                                            ("widget login", "FAIL", "signed out")])
        results = collect(Fake())
        self.assertEqual([r["name"] for r in results], ["widget binary", "widget login"])
        self.assertIn("FAIL  widget login   signed out", format_report(results))

    def test_a_check_dataclass_and_its_dict_carry_the_same_three_fields(self):
        value = {"name": "n", "status": "OK", "detail": "d"}
        self.assertEqual(Check.from_dict(value).as_dict(), value)


class TheCodexProviderIsReachableThroughTheRegistry(unittest.TestCase):
    def test_get_returns_the_codex_provider_with_every_surface_it_has(self):
        provider = providers.get("codex")
        self.assertEqual(type(provider).__name__, "CodexProvider")
        self.assertEqual(provider.name, "codex")
        self.assertLessEqual({"live_switch", "path_wrapper", "per_account_home",
                              "desktop_app", "openclaw_sync"}, provider.capabilities)

    def test_the_default_provider_is_the_codex_one_and_is_cached(self):
        self.assertIs(providers.get(), providers.get("codex"))
        self.assertEqual(providers.DEFAULT, "codex")
        self.assertIn("codex", providers.names())

    def test_an_unknown_provider_is_a_lookup_error_that_names_what_exists(self):
        with self.assertRaises(LookupError) as caught:
            providers.get("nonesuch")
        self.assertIn("codex", str(caught.exception))

    def test_the_codex_provider_knows_where_its_quota_lives(self):
        provider = providers.get("codex")
        self.assertEqual(provider.quota_shape.bucket_id, "codex")
        self.assertEqual((provider.quota_shape.short_minutes, provider.quota_shape.weekly_minutes),
                         (300, 10080))

    def test_the_fake_satisfies_the_protocol_the_codex_provider_satisfies(self):
        from xswap.providers.base import Provider
        self.assertIsInstance(FakeProvider(), Provider)
        self.assertIsInstance(providers.get("codex"), Provider)

    def test_credential_state_is_the_neutral_three_way_answer(self):
        fake = FakeProvider(accounts={"/x": {"state": CredentialState.SIGN_IN_REQUIRED}})
        self.assertFalse(fake.credential_state(Path("/x")).ok)
        self.assertTrue(fake.credential_state(Path("/unknown")).ok)


if __name__ == "__main__":
    unittest.main()


def test_tick_does_not_ask_a_provider_without_live_switch_to_move_sessions(capsys):
    """A provider that cannot move a running session must not be asked to.

    The selection still moves (the next launch picks it up); only the bridge
    signalling is skipped. A fake lacking the method would otherwise crash tick.
    """
    from unittest.mock import patch

    from xswap.core import tick
    from xswap.core.types import LIVE_SWITCH

    class Stuck(FakeProvider):
        capabilities = frozenset(FakeProvider.capabilities) - {LIVE_SWITCH}

        def switch_running(self, manager, name):
            raise AssertionError("switch_running called without the live_switch capability")

    provider = Stuck()
    assert LIVE_SWITCH not in provider.capabilities

    class Manager:
        def read(self):
            return {"active": "a"}

        def enabled_accounts(self):
            return [("a", None), ("b", None)]

        def account_rows(self, max_age=None):
            return [row("a", weekly=0, active=True), row("b", weekly=90)]

        def use_if_active(self, current, target):
            self.moved = (current, target)
            return True

    manager = Manager()
    manager.provider = provider
    with patch.object(tick, "read_settings", return_value={"enabled": True, "accounts": ["a", "b"], "weeklyRemainingThreshold": 10}), \
            patch.object(tick, "is_auth_failed", return_value=False):
        code = tick.run_tick(manager)
    assert code == tick.EXIT_SWITCHED
    assert manager.moved == ("a", "b")
    assert "switched:" in capsys.readouterr().out
