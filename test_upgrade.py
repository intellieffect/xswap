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
             contextlib.redirect_stdout(io.StringIO()) as out:
            result = upgrade("0.4.2")
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        self.assertIn("xswap 0.5.0", out.getvalue())


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
