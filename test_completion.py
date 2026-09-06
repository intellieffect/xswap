import contextlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_swap
import xswap_completion
from codex_swap import main

# Subcommand -> whether its FIRST positional is an existing-account name
# (per Acceptance in INT-4908). "run" has no such positional; its hook is
# the --account option instead, checked separately.
POSITIONAL_HOOK_SUBCOMMANDS = ("use", "remove", "login", "disable", "enable", "map", "usage", "app")


def _zsh_function_body(script, canonical_name):
    match = re.search(
        r"_xswap_args_" + re.escape(canonical_name.replace("-", "_")) + r"\(\) \{(.*?)\n\}",
        script,
        re.S,
    )
    assert match, f"no _xswap_args_{canonical_name} function found"
    return match.group(1)


def _bash_case_block(script, name):
    # Case labels are pipe-separated alias groups like "use|switch)"; match
    # `name` as a whole alternative, not a substring (e.g. "disable" must
    # not match inside "auto-disable").
    match = re.search(
        r"\n    ((?:[\w-]+\|)*" + re.escape(name) + r"(?:\|[\w-]+)*)\)\n(.*?)\n      ;;",
        script,
        re.S,
    )
    assert match, f"no bash case block for {name!r} found"
    return match.group(2)


class GenerateInputValidationTests(unittest.TestCase):
    def test_rejects_unsupported_shell(self):
        with self.assertRaises(ValueError):
            xswap_completion.generate(codex_swap.parser(), "fish")


class ZshScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = xswap_completion.generate(codex_swap.parser(), "zsh")

    def test_starts_with_compdef_header(self):
        self.assertTrue(self.script.startswith("#compdef xswap"))
        # Two-line comment telling the user where to put it (Scope item 2).
        head = self.script.splitlines()[:3]
        self.assertTrue(any("fpath" in line for line in head))
        self.assertTrue(any("eval" in line and ".zshrc" in line for line in head))

    def test_passes_zsh_syntax_check(self):
        zsh = shutil.which("zsh")
        if not zsh:
            self.skipTest("zsh binary not available")
        with tempfile.NamedTemporaryFile("w", suffix=".zsh", delete=False) as fh:
            fh.write(self.script)
            path = fh.name
        try:
            result = subprocess.run([zsh, "-n", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)

    def test_every_subcommand_name_appears(self):
        sub_action = codex_swap.parser()._subparsers._group_actions[0]
        for name in sub_action.choices:
            self.assertIn(name, self.script, name)

    def test_list_and_run_long_options_appear(self):
        sub_action = codex_swap.parser()._subparsers._group_actions[0]
        for command in ("list", "run"):
            for action in sub_action.choices[command]._actions:
                for option in action.option_strings:
                    if option.startswith("--"):
                        self.assertIn(option, self.script, f"{command} {option}")

    def test_account_hook_present_on_positional_subcommands(self):
        for name in POSITIONAL_HOOK_SUBCOMMANDS:
            body = _zsh_function_body(self.script, name)
            self.assertIn("_xswap_account_names", body, name)

    def test_account_hook_present_on_run_account_option(self):
        body = _zsh_function_body(self.script, "run")
        self.assertIn("--account=[", body)
        self.assertIn("_xswap_account_names", body)

    def test_account_hook_absent_on_register_and_add(self):
        # register/add create a *new* label; there is nothing to complete.
        for name in ("register", "add"):
            body = _zsh_function_body(self.script, name)
            self.assertNotIn("_xswap_account_names", body, name)

    def test_completion_subcommand_offers_shell_choices(self):
        body = _zsh_function_body(self.script, "completion")
        self.assertIn("zsh", body)
        self.assertIn("bash", body)

    def test_account_names_hook_reads_offline_json_only(self):
        self.assertIn("xswap list --offline --json", self.script)
        # No network / live quota fetch, and failures are swallowed silently.
        self.assertIn("2>/dev/null", self.script)


class BashScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = xswap_completion.generate(codex_swap.parser(), "bash")

    def test_registers_completion_function(self):
        self.assertIn("complete -F _xswap xswap", self.script)

    def test_header_mentions_bashrc(self):
        head = "\n".join(self.script.splitlines()[:3])
        self.assertIn("eval", head)
        self.assertIn(".bashrc", head)

    def test_passes_bash_syntax_check(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash binary not available")
        with tempfile.NamedTemporaryFile("w", suffix=".bash", delete=False) as fh:
            fh.write(self.script)
            path = fh.name
        try:
            result = subprocess.run([bash, "-n", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)

    def test_every_subcommand_name_appears(self):
        sub_action = codex_swap.parser()._subparsers._group_actions[0]
        for name in sub_action.choices:
            self.assertIn(name, self.script, name)

    def test_list_and_run_long_options_appear(self):
        sub_action = codex_swap.parser()._subparsers._group_actions[0]
        for command in ("list", "run"):
            for action in sub_action.choices[command]._actions:
                for option in action.option_strings:
                    if option.startswith("--"):
                        self.assertIn(option, self.script, f"{command} {option}")

    def test_account_hook_present_on_positional_subcommands(self):
        for name in POSITIONAL_HOOK_SUBCOMMANDS:
            block = _bash_case_block(self.script, name)
            self.assertIn("_xswap_account_names", block, name)

    def test_account_hook_present_on_run_account_option(self):
        block = _bash_case_block(self.script, "run")
        self.assertIn('"$prev" == "--account"', block)
        self.assertIn("_xswap_account_names", block)

    def test_account_hook_absent_on_register_and_add(self):
        for name in ("register", "add"):
            block = _bash_case_block(self.script, name)
            self.assertNotIn("_xswap_account_names", block, name)

    def test_use_and_switch_alias_share_the_hook(self):
        block = _bash_case_block(self.script, "switch")
        self.assertIn("_xswap_account_names", block)


class CliDispatchTests(unittest.TestCase):
    """These exercise `xswap completion ...` through main(), which builds a
    Manager() unconditionally, so CODEX_SWAP_HOME/CODEX_HOME must point at a
    throwaway temp dir per the "never touch the real home" rule."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.env = {"CODEX_SWAP_HOME": str(base / "store"), "CODEX_HOME": str(base / "home")}

    def test_completion_zsh_prints_script_and_exits_zero(self):
        out = io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
            code = main(["completion", "zsh"])
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith("#compdef xswap"))

    def test_completion_bash_prints_script_and_exits_zero(self):
        out = io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
            code = main(["completion", "bash"])
        self.assertEqual(code, 0)
        self.assertIn("complete -F _xswap xswap", out.getvalue())

    def test_completion_fish_is_rejected_with_a_clear_error(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             self.assertRaises(SystemExit) as ctx:
            main(["completion", "fish"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("invalid choice: 'fish'", err.getvalue())
        self.assertIn("zsh", err.getvalue())
        self.assertIn("bash", err.getvalue())

    def test_completion_never_touches_real_home_directories(self):
        untouched = Path(self.env["CODEX_SWAP_HOME"])
        self.assertFalse(untouched.exists())
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()):
            main(["completion", "zsh"])
        # Manager() still creates its (temp, throwaway) store dir as a side
        # effect of construction; the point is it is this temp dir, not
        # ~/.local/share/codex-swap or ~/.codex.
        self.assertTrue(untouched.exists())


if __name__ == "__main__":
    unittest.main()
