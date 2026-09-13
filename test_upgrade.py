import contextlib
import io
import pathlib
import subprocess
import tomllib
import unittest
from unittest.mock import patch

from xswap_upgrade import UpgradeError, choose, install_command, list_tags, upgrade

LS_REMOTE_OUTPUT = "\n".join([
    "abc123\trefs/tags/v0.4.2",
    "def456\trefs/tags/v0.5.0",
    "aaa111\trefs/tags/not-a-version",
    "bbb222\trefs/tags/v1.2",
    "ccc333\trefs/tags/v0.4.2^{}",
]) + "\n"


class ListTagsTests(unittest.TestCase):
    def test_ignores_non_semver_tags_and_sorts_by_version(self):
        run = lambda *a, **k: subprocess.CompletedProcess([], 0, LS_REMOTE_OUTPUT, "")
        tags = list_tags(run=run)
        self.assertEqual(tags, [((0, 4, 2), "v0.4.2"), ((0, 5, 0), "v0.5.0")])

    def test_choose_picks_max_version_by_default(self):
        run = lambda *a, **k: subprocess.CompletedProcess([], 0, LS_REMOTE_OUTPUT, "")
        self.assertEqual(choose(list_tags(run=run)), ((0, 5, 0), "v0.5.0"))

    def test_nonzero_exit_raises(self):
        run = lambda *a, **k: subprocess.CompletedProcess([], 128, "", "fatal: unable to access")
        with self.assertRaises(UpgradeError):
            list_tags(run=run)

    def test_timeout_raises(self):
        def run(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=30)
        with self.assertRaises(UpgradeError):
            list_tags(run=run)


class ChooseTests(unittest.TestCase):
    def setUp(self):
        self.tags = [((0, 4, 2), "v0.4.2"), ((0, 5, 0), "v0.5.0")]

    def test_requested_tag_missing_raises(self):
        with self.assertRaises(UpgradeError):
            choose(self.tags, requested="v9.9.9")

    def test_requested_tag_found(self):
        self.assertEqual(choose(self.tags, requested="v0.4.2"), ((0, 4, 2), "v0.4.2"))


class InstallCommandTests(unittest.TestCase):
    def test_uses_exact_repo_url_form(self):
        self.assertEqual(install_command("v0.5.0"),
                          ["uv", "tool", "install", "--force", "git+https://github.com/intellieffect/xswap.git@v0.5.0"])


class UpgradeTests(unittest.TestCase):
    def test_already_up_to_date_returns_zero_without_installing(self):
        with patch("xswap_upgrade.list_tags", return_value=[((0, 4, 2), "v0.4.2")]), \
             patch("xswap_upgrade.subprocess.run") as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            result = upgrade("0.4.2")
        self.assertEqual(result, 0)
        run.assert_not_called()
        self.assertIn("already up to date", out.getvalue())

    def test_dry_run_prints_command_without_installing(self):
        with patch("xswap_upgrade.list_tags", return_value=[((0, 5, 0), "v0.5.0")]), \
             patch("xswap_upgrade.shutil.which", return_value="/usr/bin/uv"), \
             patch("xswap_upgrade.subprocess.run") as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            result = upgrade("0.4.2", dry=True)
        self.assertEqual(result, 0)
        run.assert_not_called()
        self.assertIn("git+https://github.com/intellieffect/xswap.git@v0.5.0", out.getvalue())

    def test_missing_uv_prints_command_and_fails(self):
        with patch("xswap_upgrade.list_tags", return_value=[((0, 5, 0), "v0.5.0")]), \
             patch("xswap_upgrade.shutil.which", return_value=None), \
             patch("xswap_upgrade.subprocess.run") as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            result = upgrade("0.4.2")
        self.assertEqual(result, 1)
        run.assert_not_called()
        self.assertIn("uv tool install", out.getvalue())

    def test_dry_run_without_uv_returns_zero(self):
        with patch("xswap_upgrade.list_tags", return_value=[((0, 5, 0), "v0.5.0")]), \
             patch("xswap_upgrade.shutil.which", return_value=None), \
             patch("xswap_upgrade.subprocess.run") as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            result = upgrade("0.4.2", dry=True)
        self.assertEqual(result, 0)
        run.assert_not_called()
        self.assertIn("git+https://github.com/intellieffect/xswap.git@v0.5.0", out.getvalue())

    def test_requested_tag_missing_raises(self):
        with patch("xswap_upgrade.list_tags", return_value=[((0, 4, 2), "v0.4.2")]):
            with self.assertRaises(UpgradeError):
                upgrade("0.4.2", tag="v9.9.9")

    def test_install_runs_and_reports_new_version(self):
        responses = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, "xswap 0.5.0\n", "")]
        with patch("xswap_upgrade.list_tags", return_value=[((0, 5, 0), "v0.5.0")]), \
             patch("xswap_upgrade.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), \
             patch("xswap_upgrade.subprocess.run", side_effect=responses) as run, \
             patch("xswap_upgrade.running_session_hints", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            result = upgrade("0.4.2")
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn("xswap 0.5.0", out.getvalue())
        self.assertNotIn("previous bridge", out.getvalue())

    def test_install_lists_running_sessions_with_their_reopen_hints(self):
        responses = [subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, "xswap 0.5.0\n", "")]
        hints = ["CLI · ai · bridge 0.4.2 · reopen with codex resume 00000000-0000-4000-8000-000000000001 to load 0.5.0",
                 "CLI · ai · bridge 0.4.2 · exit and reopen it to load 0.5.0",
                 "Desktop · work · bridge 0.4.2 · quit and reopen it with xswap app to load 0.5.0"]
        with patch("xswap_upgrade.list_tags", return_value=[((0, 5, 0), "v0.5.0")]), \
             patch("xswap_upgrade.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), \
             patch("xswap_upgrade.subprocess.run", side_effect=responses), \
             patch("xswap_upgrade.running_session_hints", return_value=hints) as gather, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(upgrade("0.4.2"), 0)
        gather.assert_called_once_with("0.5.0")
        self.assertEqual(out.getvalue().splitlines()[:4], [
            "3 running xswap session(s) still use the previous bridge; reopen them to load v0.5.0:",
            "  " + hints[0], "  " + hints[1], "  " + hints[2]])

    def test_session_records_are_read_before_the_reinstall_replaces_the_code(self):
        order = []

        def run(command, **kwargs):
            order.append("install" if command[:3] == ["uv", "tool", "install"] else "version")
            return subprocess.CompletedProcess([], 0, "xswap 0.5.0\n", "")

        def gather(target):
            order.append("gather")
            return []

        with patch("xswap_upgrade.list_tags", return_value=[((0, 5, 0), "v0.5.0")]), \
             patch("xswap_upgrade.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), \
             patch("xswap_upgrade.subprocess.run", side_effect=run), \
             patch("xswap_upgrade.running_session_hints", side_effect=gather), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(upgrade("0.4.2"), 0)
        self.assertEqual(order, ["gather", "install", "version"])

    def test_running_session_hints_come_from_the_store_and_carry_no_secrets(self):
        import fcntl, os, tempfile
        from codex_swap import atomic_json
        from xswap_upgrade import running_session_hints
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp).resolve()
            (base / "codex-home").mkdir()
            store = base / "store"
            run_dir = store / "auto" / "cli-runs" / "old"
            run_dir.mkdir(parents=True)
            atomic_json(run_dir / "status.json", {"account": "ai", "bridgeVersion": "0.7.8", "updatedAt": 0,
                                                  "accessToken": "must-not-leak"})
            fd = os.open(run_dir / ".bridge.lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with patch.dict(os.environ, {"CODEX_SWAP_HOME": str(store), "CODEX_HOME": str(base / "codex-home")}):
                    hints = running_session_hints("0.8.0")
            finally:
                os.close(fd)
        self.assertEqual(hints, ["CLI · ai · bridge 0.7.8 · exit and reopen it to load 0.8.0"])

    def test_readme_install_snippets_pin_the_current_release(self):
        # README pinned v0.6.1 through four releases; keep the snippets on __version__ (INT-5085).
        import re
        from codex_swap import __version__
        root = pathlib.Path(__file__).parent
        for name in ("README.md", "README.ko.md"):
            tags = set(re.findall(r"xswap\.git@v(\d+\.\d+\.\d+)", (root / name).read_text()))
            self.assertEqual(tags, {__version__}, name)


if __name__ == "__main__":
    unittest.main()


class VersionSingleSourceTests(unittest.TestCase):
    def test_pyproject_and_module_version_match(self):
        # `xswap --version` and `xswap upgrade` read codex_swap.__version__; uv installs
        # pyproject's version. A release that bumps only one of them ships a wrong
        # version string, or an upgrade that thinks it is already current.
        import codex_swap
        pyproject = pathlib.Path(__file__).with_name("pyproject.toml")
        with pyproject.open("rb") as stream:
            declared = tomllib.load(stream)["project"]["version"]
        self.assertEqual(codex_swap.__version__, declared)


class PackagedModulesTests(unittest.TestCase):
    def test_every_top_level_module_is_in_py_modules(self):
        # uv/pip install only the modules listed in pyproject's py-modules; a new
        # xswap_*.py that is not listed imports fine from a checkout but raises
        # ModuleNotFoundError from an installed wheel.
        root = pathlib.Path(__file__).parent
        with (root / "pyproject.toml").open("rb") as stream:
            listed = set(tomllib.load(stream)["tool"]["setuptools"]["py-modules"])
        present = {p.stem for p in root.glob("*.py") if not p.name.startswith("test_")}
        self.assertEqual(sorted(present - listed), [])
