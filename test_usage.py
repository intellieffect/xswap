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

from codex_swap import Manager, atomic_json
from xswap_usage import UsageError, normalize_limits, read_limits, reset_label, usage_lines


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


if __name__ == "__main__":
    unittest.main()
