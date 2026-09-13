import contextlib
import io
import json
import unittest
from unittest.mock import patch

import test_codex_swap
from test_codex_swap import dual_window_raw
from codex_swap import Manager, atomic_json, main, rank_candidates
from xswap_usage import AUTH_FAILED_STATUS, UsageError, normalize_limits
from xswap_tick import decide

REPORT = dict(applied=0, pending=0, unsupported=0, failed=0, unconfirmed=0)


def row(name, raw=None, status="ok", disabled=False, active=False):
    """A Manager.account_rows()-shaped row from a raw rateLimits response (or none)."""
    buckets = normalize_limits(raw) if raw is not None else []
    return {"name": name, "identity": f"{name}@example.test", "status": status, "buckets": buckets,
            "resetCredits": None, "fetchedAt": None, "disabled": disabled, "cached": False, "slot": 1, "active": active}


class RankCandidatesTests(unittest.TestCase):
    def test_weekly_reserve_excludes_candidates_at_or_below_it(self):
        rows = [row("a", dual_window_raw(remaining7d=10)), row("b", dual_window_raw(remaining7d=10.5))]
        self.assertEqual(rank_candidates(rows, weekly_remaining=10)[0], "b")
        self.assertIsNone(rank_candidates(rows, weekly_remaining=10.5)[0])
        self.assertEqual(rank_candidates(rows)[0], "b")  # best_account's default: exhaustion only

    def test_summary_lists_every_considered_row_without_tokens(self):
        rows = [row("a", dual_window_raw(remaining7d=10)), row("b", status="offline"), row("c", dual_window_raw(remaining7d=5), disabled=True)]
        name, detail = rank_candidates(rows, weekly_remaining=10)
        self.assertIsNone(name)
        self.assertEqual([c["name"] for c in detail["candidates"]], ["a", "b"])
        self.assertNotIn("token", json.dumps(detail))


class DecideTests(unittest.TestCase):
    def test_at_or_below_reserve_switches_to_the_best_pool_account(self):
        rows = [row("main", dual_window_raw(remaining5h=90, remaining7d=10)),
                row("second", dual_window_raw(remaining5h=90, remaining7d=40)),
                row("third", dual_window_raw(remaining5h=90, remaining7d=80))]
        outcome = decide(rows, "main", ["main", "second", "third"], 10)
        self.assertEqual((outcome["decision"], outcome["target"]), ("switch", "third"))
        self.assertEqual(outcome["message"], "main -> third (main weekly 10% left, at or below the 10% reserve; third weekly 80% left)")
        self.assertEqual(outcome["remaining"], {"main": 10, "second": 40, "third": 80})

    def test_just_above_reserve_is_no_action(self):
        rows = [row("main", dual_window_raw(remaining7d=10.1)), row("second", dual_window_raw(remaining7d=90))]
        outcome = decide(rows, "main", ["main", "second"], 10)
        self.assertEqual((outcome["decision"], outcome["reason"]), ("no-action", "above-reserve"))
        self.assertEqual(outcome["message"], "no-action: main weekly 10.1% left is above the 10% reserve")

    def test_unknown_current_quota_never_switches_even_with_a_healthy_alternative(self):
        rows = [row("main", status="usage unavailable: usage request timed out"), row("second", dual_window_raw(remaining7d=90))]
        outcome = decide(rows, "main", ["main", "second"], 10)
        self.assertEqual((outcome["decision"], outcome["reason"]), ("no-action", "quota-unknown"))
        self.assertEqual(outcome["message"], "no-action: main quota unknown (usage unavailable: usage request timed out); not switching on unknown quota")
        no_weekly = [row("main", {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 50, "windowDurationMins": 300}}}), rows[1]]
        self.assertEqual(decide(no_weekly, "main", ["main", "second"], 10)["message"],
                         "no-action: main quota unknown (weekly window not reported); not switching on unknown quota")

    def test_sign_in_required_current_switches(self):
        rows = [row("main", status="usage unavailable: sign in again to read usage"), row("second", dual_window_raw(remaining7d=90))]
        outcome = decide(rows, "main", ["main", "second"], 10)
        self.assertEqual(outcome["target"], "second")
        self.assertEqual(outcome["message"], "main -> second (main sign-in required; second weekly 90% left)")
        self.assertEqual(decide([row("main", status="not signed in"), rows[1]], "main", ["main", "second"], 10)["target"], "second")

    def test_cached_auth_failed_status_switches(self):
        # `alert --auto-switch` always runs with --cached, so a rejected login reaches the
        # tick as AUTH_FAILED_STATUS and nothing else; it must count as unusable, not unknown.
        rows = [row("main", status=AUTH_FAILED_STATUS), row("second", dual_window_raw(remaining7d=90))]
        outcome = decide(rows, "main", ["main", "second"], 10)
        self.assertEqual(outcome["target"], "second")
        self.assertEqual(outcome["message"], "main -> second (main sign-in required; second weekly 90% left)")

    def test_auth_failed_current_switches_and_auth_failed_candidate_is_skipped(self):
        rows = [row("main", dual_window_raw(remaining7d=90)), row("second", dual_window_raw(remaining7d=95)), row("third", dual_window_raw(remaining7d=60))]
        outcome = decide(rows, "main", ["main", "second", "third"], 10, failed=frozenset({"main", "second"}))
        self.assertEqual((outcome["decision"], outcome["target"]), ("switch", "third"))

    def test_reached_and_exhausted_short_window_count_as_below_reserve(self):
        reached = [row("main", dual_window_raw(remaining7d=90, reached="primary")), row("second", dual_window_raw(remaining7d=50))]
        self.assertEqual(decide(reached, "main", ["main", "second"], 10)["message"],
                         "main -> second (main limit reached (primary); second weekly 50% left)")
        short = [row("main", dual_window_raw(remaining5h=0, remaining7d=90)), row("second", dual_window_raw(remaining7d=50))]
        self.assertEqual(decide(short, "main", ["main", "second"], 10)["message"],
                         "main -> second (main has an exhausted codex window; second weekly 50% left)")

    def test_blocked_lists_pool_candidates_and_ignores_accounts_outside_the_pool(self):
        rows = [row("main", dual_window_raw(remaining7d=5)), row("second", dual_window_raw(remaining7d=10)),
                row("third", status="offline"), row("outside", dual_window_raw(remaining7d=99))]
        outcome = decide(rows, "main", ["main", "second", "third"], 10)
        self.assertEqual((outcome["decision"], outcome["reason"], outcome["target"]), ("blocked", "no-candidate", None))
        self.assertEqual(outcome["message"], "blocked: main weekly 5% left, at or below the 10% reserve, but no pool account is above the 10% reserve (second 10%, third unknown)")

    def test_disabled_candidate_is_skipped(self):
        rows = [row("main", dual_window_raw(remaining7d=0)), row("second", dual_window_raw(remaining7d=90), disabled=True, status="disabled")]
        outcome = decide(rows, "main", ["main", "second"], 0)
        self.assertEqual(outcome["decision"], "blocked")
        self.assertTrue(outcome["message"].endswith("(none)"), outcome["message"])

    def test_reserve_zero_only_switches_on_exhaustion(self):
        rows = [row("main", dual_window_raw(remaining7d=1)), row("second", dual_window_raw(remaining7d=90))]
        self.assertEqual(decide(rows, "main", ["main", "second"], 0)["reason"], "above-reserve")
        rows[0] = row("main", dual_window_raw(remaining7d=0))
        self.assertEqual(decide(rows, "main", ["main", "second"], 0)["target"], "second")

    def test_missing_selection_row_is_blocked(self):
        self.assertEqual(decide([row("second", dual_window_raw())], "main", ["main", "second"], 10)["reason"], "no-selection")

    def test_switching_never_reverses_without_a_reset(self):
        # No hysteresis needed: after main -> second, the same numbers are a no-action for second,
        # and second can only hand back once main is above the reserve again (its weekly reset).
        rows = [row("main", dual_window_raw(remaining7d=10)), row("second", dual_window_raw(remaining7d=80))]
        self.assertEqual(decide(rows, "main", ["main", "second"], 10)["target"], "second")
        self.assertEqual(decide(rows, "second", ["second", "main"], 10)["reason"], "above-reserve")
        rows[1] = row("second", dual_window_raw(remaining7d=10))
        self.assertEqual(decide(rows, "second", ["second", "main"], 10)["decision"], "blocked")
        rows[0] = row("main", dual_window_raw(remaining7d=100))  # main's weekly reset
        self.assertEqual(decide(rows, "second", ["second", "main"], 10)["target"], "main")


class AutoTickCommandTests(unittest.TestCase):
    setUp = test_codex_swap.BestAccountTests.setUp
    add = test_codex_swap.BestAccountTests.add
    fake_read_limits = test_codex_swap.BestAccountTests.fake_read_limits

    def enable(self, pool="main,second", reserve=10, enabled=True):
        atomic_json(self.manager.root / "auto.json",
                    {"enabled": enabled, "accounts": pool.split(","), "weeklyRemainingThreshold": reserve})

    def snapshot(self):
        return self.manager.registry.read_bytes(), (self.manager.root / "auto.json").read_bytes()

    def tick(self, mapping, argv=(), report=REPORT):
        fake = self.fake_read_limits(mapping)
        out, err = io.StringIO(), io.StringIO()
        with patch("codex_swap.Manager", return_value=self.manager), \
             patch("codex_swap.read_limits", side_effect=fake) as fetch, \
             patch.object(Manager, "codex", return_value="codex"), \
             patch("xswap_switch.switch_running", return_value=report) as broadcast, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["auto-tick", *argv])
        return code, out.getvalue(), err.getvalue(), fetch, broadcast

    def test_switches_through_the_use_path(self):
        self.add("second")
        self.enable()
        report = dict(applied=1, pending=0, unsupported=0, failed=0, unconfirmed=0)
        code, out, _, fetch, broadcast = self.tick({"main": dual_window_raw(remaining7d=8), "second": dual_window_raw(remaining7d=80)}, report=report)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "switched: main -> second (main weekly 8% left, at or below the 10% reserve; second weekly 80% left)",
            "Running bridged sessions: applied 1."])
        self.assertEqual(self.manager.read()["active"], "second")
        self.assertEqual(json.loads((self.manager.root / "auto.json").read_text())["accounts"], ["second", "main"])
        broadcast.assert_called_once_with(self.manager, "second")
        self.assertEqual(fetch.call_count, 2)
        self.assertNotIn("fake-token", out)

    def test_no_action_above_reserve_leaves_everything_unchanged(self):
        self.add("second")
        self.enable()
        before = self.snapshot()
        code, out, _, _, broadcast = self.tick({"main": dual_window_raw(remaining7d=11), "second": dual_window_raw(remaining7d=90)})
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "no-action: main weekly 11% left is above the 10% reserve")
        broadcast.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_reports_would_switch_and_writes_nothing(self):
        self.add("second")
        self.enable()
        before = self.snapshot()
        code, out, _, _, broadcast = self.tick({"main": dual_window_raw(remaining7d=5), "second": dual_window_raw(remaining7d=80)}, argv=["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "would-switch: main -> second (main weekly 5% left, at or below the 10% reserve; second weekly 80% left)")
        broadcast.assert_not_called()
        self.assertEqual(self.manager.read()["active"], "main")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.manager.root.rglob("switch.json")), [])

    def test_blocked_when_auto_switching_is_disabled_without_spawning_codex(self):
        self.add("second")
        self.enable(enabled=False)
        code, out, _, fetch, broadcast = self.tick({"main": dual_window_raw(remaining7d=5), "second": dual_window_raw(remaining7d=80)})
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), "blocked: automatic switching is not enabled; run: xswap auto-enable --accounts main,second")
        fetch.assert_not_called()
        broadcast.assert_not_called()

    def test_blocked_when_nothing_is_selected(self):
        self.add("second")
        self.enable()
        data = self.manager.read()
        data["active"] = None
        atomic_json(self.manager.registry, data)
        code, out, _, fetch, _ = self.tick({"main": dual_window_raw(remaining7d=5), "second": dual_window_raw(remaining7d=80)})
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), "blocked: no account is selected; run: xswap use NAME")
        fetch.assert_not_called()

    def test_blocked_when_no_pool_account_is_above_reserve_even_if_an_outside_account_is(self):
        self.add("second")
        self.add("third")
        self.enable("main,second")
        code, out, _, _, broadcast = self.tick({"main": dual_window_raw(remaining7d=5), "second": dual_window_raw(remaining7d=10), "third": dual_window_raw(remaining7d=99)})
        self.assertEqual(code, 3)
        self.assertEqual(out.strip(), "blocked: main weekly 5% left, at or below the 10% reserve, but no pool account is above the 10% reserve (second 10%)")
        self.assertEqual(self.manager.read()["active"], "main")
        broadcast.assert_not_called()

    def test_unknown_current_quota_is_no_action(self):
        self.add("second")
        self.enable()
        code, out, _, _, broadcast = self.tick({"main": UsageError("usage request timed out"), "second": dual_window_raw(remaining7d=90)})
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "no-action: main quota unknown (usage unavailable: usage request timed out); not switching on unknown quota")
        self.assertEqual(self.manager.read()["active"], "main")
        broadcast.assert_not_called()

    def test_sign_in_required_current_is_switched_away(self):
        self.add("second")
        self.enable()
        code, out, _, _, broadcast = self.tick({"main": UsageError("sign in again to read usage"), "second": dual_window_raw(remaining7d=90)})
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "switched: main -> second (main sign-in required; second weekly 90% left)")
        self.assertEqual(self.manager.read()["active"], "second")
        broadcast.assert_called_once_with(self.manager, "second")

    def test_auth_failed_hook_excludes_candidates(self):
        self.add("second")
        self.add("third")
        self.enable("main,second,third")
        mapping = {"main": dual_window_raw(remaining7d=5), "second": dual_window_raw(remaining7d=95), "third": dual_window_raw(remaining7d=60)}
        with patch("xswap_tick.is_auth_failed", side_effect=lambda manager, name: name == "second"):
            code, out, _, _, _ = self.tick(mapping)
        self.assertEqual(code, 0)
        self.assertEqual(self.manager.read()["active"], "third")
        self.assertTrue(out.startswith("switched: main -> third ("), out)

    def test_disabled_pool_account_is_never_fetched_or_selected(self):
        self.add("second")
        self.enable()
        self.manager.set_disabled("second", True)
        code, out, _, fetch, _ = self.tick({"main": dual_window_raw(remaining7d=5)})
        self.assertEqual(code, 3)
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(out.strip().endswith("(none)"), out)
        self.assertEqual(self.manager.read()["active"], "main")

    def test_cached_reuses_a_fresh_entry_without_spawning_codex(self):
        self.add("second")
        self.enable()
        mapping = {"main": dual_window_raw(remaining7d=50), "second": dual_window_raw(remaining7d=90)}
        code, _, _, fetch, _ = self.tick(mapping, argv=["--cached", "60"])
        self.assertEqual((code, fetch.call_count), (2, 2))
        code, _, _, fetch, _ = self.tick(mapping, argv=["--cached", "60"])
        self.assertEqual(code, 2)
        fetch.assert_not_called()

    def test_invalid_cached_is_rejected_before_any_fetch(self):
        self.enable()
        code, _, err, fetch, _ = self.tick({}, argv=["--cached", "0"])
        self.assertEqual(code, 1)
        self.assertIn("--cached SECONDS must be a positive number", err)
        fetch.assert_not_called()

    def test_json_output_shape(self):
        self.add("second")
        self.enable()
        code, out, _, _, _ = self.tick({"main": dual_window_raw(remaining7d=50), "second": dual_window_raw(remaining7d=90)}, argv=["--json"])
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"decision", "reason", "exitCode", "selected", "target", "reserve", "dryRun", "remaining", "candidates", "report", "lines"})
        self.assertEqual((payload["decision"], payload["reason"], payload["exitCode"], payload["selected"], payload["target"], payload["reserve"]),
                         ("no-action", "above-reserve", 2, "main", None, 10.0))
        self.assertEqual(payload["remaining"], {"main": 50, "second": 90})
        self.assertNotIn("fake-token", out)

    def test_json_switch_carries_the_report(self):
        self.add("second")
        self.enable()
        code, out, _, _, _ = self.tick({"main": dual_window_raw(remaining7d=5), "second": dual_window_raw(remaining7d=90)}, argv=["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual((payload["decision"], payload["target"], payload["report"]), ("switched", "second", REPORT))
        self.assertEqual(payload["lines"][1], "No running bridged sessions; new sessions start as second.")

    def test_selection_changed_during_fetch_is_not_overridden(self):
        self.add("second")
        self.add("third")
        self.enable("main,second,third")
        fake = self.fake_read_limits({"main": dual_window_raw(remaining7d=5), "second": dual_window_raw(remaining7d=80), "third": dual_window_raw(remaining7d=70)})
        main_home = str(self.manager.account("main")[1])

        def read(codex, env, timeout=12):
            if env["CODEX_HOME"] == main_home:
                self.manager.use("third")  # a manual selection lands mid-fetch
            return fake(codex, env, timeout)

        out = io.StringIO()
        with patch("codex_swap.Manager", return_value=self.manager), patch("codex_swap.read_limits", side_effect=read), \
             patch.object(Manager, "codex", return_value="codex"), \
             patch("xswap_switch.switch_running", return_value=REPORT) as broadcast, contextlib.redirect_stdout(out):
            code = main(["auto-tick"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().strip(), "no-action: the selection changed from main while quota was being read; nothing changed")
        self.assertEqual(self.manager.read()["active"], "third")
        broadcast.assert_not_called()

    def test_use_best_is_unchanged_by_the_refactor(self):
        self.add("second")
        fake = self.fake_read_limits({"main": dual_window_raw(remaining5h=40, remaining7d=40), "second": dual_window_raw(remaining5h=90, remaining7d=90)})
        with patch("codex_swap.read_limits", side_effect=fake), patch.object(Manager, "codex", return_value="codex"):
            name, reason = self.manager.best_account()
        self.assertEqual((name, reason["remaining"]), ("second", {"5h": 90, "7d": 90}))


if __name__ == "__main__":
    unittest.main()
