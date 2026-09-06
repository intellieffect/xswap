import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_swap import Manager, SwapError, atomic_json, main
from xswap_usage import UsageError, normalize_limits, read_limits, reset_label, short_line, usage_lines, warnings


def response():
    return {"rateLimitsByLimitId": {
        "spark": {"limitName": "Spark", "primary": {"usedPercent": 10, "windowDurationMins": 15}},
        "codex": {"planType": "pro", "primary": {"usedPercent": 23, "windowDurationMins": 300, "resetsAt": 10000},
                  "secondary": {"usedPercent": 99, "windowDurationMins": 10080, "resetsAt": 20000}},
    }, "secret": "do-not-display"}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def fake_codex(self, result=None, error=None, stall=False):
        script = self.root / "codex"
        script.write_text(f'''#!{sys.executable}
import os, sys, json, time
from pathlib import Path
Path({str(self.root / 'pid')!r}).write_text(str(os.getpid()))
if {stall!r}: time.sleep(30)
for line in sys.stdin:
    message = json.loads(line)
    method = message['method']
    with open({str(self.root / 'methods')!r}, 'a') as f: f.write(method+'\\n')
    assert method in ['initialize', 'initialized', 'account/rateLimits/read']
    if method == 'initialize':
        # Include a notification in the same pipe write as a response.
        sys.stdout.write(json.dumps({{'id':1,'result':{{}}}})+'\\n'+json.dumps({{'method':'unrelated/notification'}})+'\\n')
        sys.stdout.flush()
    elif method == 'account/rateLimits/read':
        reply = {{'id':2, 'error':{error!r}}} if {error is not None!r} else {{'id':2,'result':{result or response()!r}}}
        print(json.dumps(reply), flush=True)
''')
        script.chmod(0o700)
        return str(script)

    def test_real_stdio_protocol_ignores_notifications_and_makes_no_model_turn(self):
        result = read_limits(self.fake_codex(), os.environ.copy(), timeout=2)
        self.assertEqual(result, response())
        self.assertEqual((self.root / "methods").read_text().splitlines(), ["initialize", "initialized", "account/rateLimits/read"])

    def test_timeout_reaps_its_child(self):
        children = []
        original = subprocess.Popen
        def start(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        with patch("xswap_usage.subprocess.Popen", side_effect=start), self.assertRaisesRegex(UsageError, "timed out"):
            read_limits(self.fake_codex(stall=True), os.environ.copy(), timeout=0.3)
        pid = children[0].pid
        self.assertIsNotNone(children[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_rpc_error_is_redacted(self):
        with self.assertRaisesRegex(UsageError, "sign in again") as caught:
            read_limits(self.fake_codex(error={"code": 401, "message": "401 unauthorized fake-secret-token"}), os.environ.copy(), timeout=2)
        self.assertNotIn("fake-secret-token", str(caught.exception))

    def test_normalization_prioritizes_codex_and_calculates_remaining(self):
        buckets = normalize_limits(response())
        self.assertEqual(buckets[0]["id"], "codex")
        self.assertEqual([w["remainingPercent"] for w in buckets[0]["windows"]], [77, 1])
        text = "\n".join(usage_lines(buckets, now=9000))
        self.assertIn("codex 5h: 77% left", text)
        self.assertIn("codex 7d: 1% left", text)
        self.assertIn("Spark 15m: 90% left", text)
        self.assertNotIn("do-not-display", json.dumps(buckets))

    def test_unknown_is_never_reported_as_unused(self):
        data = {"rateLimits": {"primary": {"windowDurationMins": 300}}}
        text = "\n".join(usage_lines(normalize_limits(data)))
        self.assertIn("remaining unknown", text)
        self.assertNotIn("100%", text)
        self.assertIn("reset unknown", text)
        self.assertEqual(usage_lines(normalize_limits({})), ["quota windows not reported"])

    def test_exhausted_limit_is_not_reset_locally_when_timestamp_passes(self):
        data = {"rateLimits": {"primary": {"usedPercent": 101, "resetsAt": 5000}}}
        text = "\n".join(usage_lines(normalize_limits(data), now=10000))
        self.assertIn("0% left", text)
        self.assertIn("reset pending", text)
        self.assertEqual(reset_label(10**100, 0), "reset unknown")

    def test_malformed_percent_is_unknown(self):
        for value in [True, float('nan'), -1, "50"]:
            buckets = normalize_limits({"rateLimits": {"primary": {"usedPercent": value}}})
            self.assertIsNone(buckets[0]["windows"][0]["remainingPercent"])

    def manager(self):
        home = self.root / "home"
        home.mkdir()
        atomic_json(home / "auth.json", {"auth_mode": "chatgpt", "tokens": {"access_token": "fake-token"}})
        manager = Manager(self.root / "store", home)
        manager.register("main")
        manager.prepare("second")
        return manager

    def test_list_fetches_only_signed_in_profiles_and_keeps_active_selection(self):
        manager = self.manager()
        with patch("codex_swap.read_limits", return_value=response()) as fetch, patch.object(manager, "codex", return_value="codex"), contextlib.redirect_stdout(io.StringIO()) as output:
            manager.show_accounts()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[1]["CODEX_HOME"], str(manager.source))
        self.assertEqual(manager.account()[0], "main")
        self.assertIn("99% 사용 · 1% 남음", output.getvalue())
        self.assertNotIn("Spark", output.getvalue())
        with patch("codex_swap.read_limits", return_value=response()), patch.object(manager, "codex", return_value="codex"), contextlib.redirect_stdout(io.StringIO()) as full:
            manager.show_accounts(include_spark=True)
        self.assertIn("Spark", full.getvalue())
        self.assertIn("로그인 필요", output.getvalue())

    def test_offline_never_starts_a_usage_server(self):
        manager = self.manager()
        with patch("codex_swap.read_limits") as fetch, contextlib.redirect_stdout(io.StringIO()):
            manager.show_accounts(offline=True)
        fetch.assert_not_called()

    def test_per_account_error_does_not_hide_other_accounts(self):
        manager = self.manager()
        with patch("codex_swap.read_limits", side_effect=UsageError("service unavailable")), patch.object(manager, "codex", return_value="codex"), contextlib.redirect_stdout(io.StringIO()) as output:
            manager.show_accounts(json_output=True)
        rows = json.loads(output.getvalue())
        self.assertEqual(len(rows), 2)
        self.assertIn("usage unavailable", rows[0]["status"])
        self.assertEqual(rows[1]["status"], "not signed in")
        self.assertNotIn("fake-token", output.getvalue())

    def row(self, buckets, status="ok", name="work", disabled=False):
        return {"name": name, "status": status, "buckets": buckets, "disabled": disabled}

    def test_warnings_below_threshold_emits_one_line_per_window(self):
        row = self.row(normalize_limits(response()))
        lines = warnings([row], 80, now=9000)
        self.assertEqual(len(lines), 2)
        self.assertIn("warn: work 5h 77% left", lines[0])
        self.assertIn("warn: work 7d 1% left", lines[1])

    def test_warnings_above_threshold_emits_nothing(self):
        row = self.row(normalize_limits(response()))
        self.assertEqual(warnings([row], 1), [])

    def test_warnings_ignores_unknown_remaining(self):
        buckets = normalize_limits({"rateLimits": {"primary": {"windowDurationMins": 300}}})
        self.assertIsNone(buckets[0]["windows"][0]["remainingPercent"])
        self.assertEqual(warnings([self.row(buckets)], 100), [])

    def test_warnings_ignores_disabled_row(self):
        row = self.row(normalize_limits(response()), disabled=True)
        self.assertEqual(warnings([row], 100), [])

    def test_warnings_ignores_non_ok_status(self):
        row = self.row(normalize_limits(response()), status="not signed in")
        self.assertEqual(warnings([row], 100), [])

    def test_main_list_warn_returns_three_and_prints_to_stderr(self):
        manager = self.manager()
        env = {"CODEX_SWAP_HOME": str(manager.root), "CODEX_HOME": str(manager.source)}
        with patch("codex_swap.read_limits", return_value=response()), patch.object(Manager, "codex", return_value="codex"), \
             patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(["list", "--warn", "20"])
        self.assertEqual(code, 3)
        self.assertIn("warn: main 7d 1% left", stderr.getvalue())
        self.assertNotIn("warn: main 5h", stderr.getvalue())

    def test_main_list_warn_above_all_windows_returns_zero(self):
        manager = self.manager()
        env = {"CODEX_SWAP_HOME": str(manager.root), "CODEX_HOME": str(manager.source)}
        with patch("codex_swap.read_limits", return_value=response()), patch.object(Manager, "codex", return_value="codex"), \
             patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(["list", "--warn", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_main_list_warn_invalid_pct_is_an_error(self):
        manager = self.manager()
        env = {"CODEX_SWAP_HOME": str(manager.root), "CODEX_HOME": str(manager.source)}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(["list", "--warn", "0"])
        self.assertEqual(code, 1)
        self.assertIn("--warn", stderr.getvalue())

    def test_main_list_warn_non_numeric_pct_is_the_same_swap_error(self):
        manager = self.manager()
        env = {"CODEX_SWAP_HOME": str(manager.root), "CODEX_HOME": str(manager.source)}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(["list", "--warn", "abc"])
        self.assertEqual(code, 1)
        self.assertIn("xswap: --warn must be a number from 1 to 100.", stderr.getvalue())

    def test_short_and_json_are_mutually_exclusive(self):
        manager = self.manager()
        with self.assertRaises(SwapError):
            manager.show_accounts(short=True, json_output=True)

    def test_short_omits_disabled_registry_entries(self):
        manager = self.manager()
        data = json.loads(manager.registry.read_text())
        data["accounts"]["second"]["disabled"] = True
        atomic_json(manager.registry, data)
        with patch("codex_swap.read_limits", return_value=response()), patch.object(manager, "codex", return_value="codex"), contextlib.redirect_stdout(io.StringIO()) as output:
            manager.show_accounts(short=True)
        self.assertEqual(output.getvalue(), "*main 77/1\n")

    def test_short_composes_with_offline(self):
        manager = self.manager()
        with patch("codex_swap.read_limits") as fetch, contextlib.redirect_stdout(io.StringIO()) as output:
            manager.show_accounts(offline=True, short=True)
        fetch.assert_not_called()
        self.assertEqual(output.getvalue(), "*main ?/? · second ?/?\n")


class ShortLineTests(unittest.TestCase):
    @staticmethod
    def bucket(primary=None, secondary=None, bucket_id="codex"):
        windows = []
        if primary is not None:
            windows.append({"position": "primary", "remainingPercent": primary})
        if secondary is not None:
            windows.append({"position": "secondary", "remainingPercent": secondary})
        return {"id": bucket_id, "name": bucket_id, "windows": windows}

    def test_three_rows_including_a_failure_and_the_active_account(self):
        rows = [
            {"name": "main", "active": True, "status": "ok", "buckets": [self.bucket(77, 12)]},
            {"name": "work", "active": False, "status": "ok", "buckets": [self.bucket(100, 98)]},
            {"name": "broken", "active": False, "status": "usage unavailable: service down", "buckets": []},
        ]
        self.assertEqual(short_line(rows), "*main 77/12 · work 100/98 · broken ?/?")

    def test_rounds_to_the_nearest_integer(self):
        rows = [{"name": "main", "active": False, "status": "ok", "buckets": [self.bucket(76.6, 11.4)]}]
        self.assertEqual(short_line(rows), "main 77/11")

    def test_unknown_window_is_a_question_mark(self):
        rows = [{"name": "main", "active": False, "status": "ok", "buckets": [self.bucket(None, None)]}]
        self.assertEqual(short_line(rows), "main ?/?")

    def test_missing_codex_bucket_is_a_question_mark(self):
        rows = [{"name": "main", "active": False, "status": "ok", "buckets": [self.bucket(90, bucket_id="spark")]}]
        self.assertEqual(short_line(rows), "main ?/?")

    def test_offline_rows_are_question_marks(self):
        rows = [
            {"name": "main", "active": True, "status": "offline", "buckets": []},
            {"name": "work", "active": False, "status": "not signed in", "buckets": []},
        ]
        self.assertEqual(short_line(rows), "*main ?/? · work ?/?")

    def test_no_accounts_is_an_empty_line(self):
        self.assertEqual(short_line([]), "")


if __name__ == "__main__":
    unittest.main()
