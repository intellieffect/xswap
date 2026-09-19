"""Byte-exact snapshot tests for `xswap`'s --help text and shell completion output.

These are a regression safety net for the INT-5610 refactor: they pin the
exact argparse-rendered help for the root parser and every subcommand, plus
the generated zsh/bash completion scripts, so an accidental change in wording,
option names, choices, or defaults during the refactor is caught even though
nothing here asserts *behavior*.

Determinism
-----------
- Help text is generated in-process via `codex_swap.parser().format_help()`
  and each subparser's own `format_help()` -- never via subprocess -- so
  there is no dependency on how the interpreter was invoked.
- `COLUMNS` is forced to "80" for the duration of generation so argparse's
  usage-line wrapping does not depend on the terminal the test happens to
  run in.
- `sys.argv[0]` is patched to a fixed literal ("xswap") because
  `codex_swap.parser()` builds its `ArgumentParser` without an explicit
  `prog=`, so argparse falls back to `os.path.basename(sys.argv[0])`; under
  `python -m unittest` that is normally "xswap" already, but under
  `pytest` or `uv run --python X.Y -m unittest` it can differ, so this is
  patched explicitly rather than relied upon.
- Completion generation is isolated from the developer's real xswap state:
  the account-name lookup in the generated *scripts* only runs at shell
  completion time (they shell out to `xswap list --offline --json`), not at
  generation time, so `xswap_completion.generate()` itself never reads local
  state. `CODEX_SWAP_HOME` is still pointed at a throwaway TemporaryDirectory
  here as a belt-and-suspenders guard in case that ever changes, and the
  generated snapshot files are grepped for known-private strings
  ("bigno", "intellieffect", "@", "/Users/", "/Volumes/") to catch any leak.

Python-version differences (checked 2026-09-19)
------------------------------------------------
Snapshots were generated and compared under `uv run --python 3.11` and the
system `python3` (3.14.6). The ROOT parser's usage line wraps differently at
COLUMNS=80: 3.11 puts the subcommand-choices brace on its own line and the
trailing `...` (for the subcommand positional) on a separate line below it;
3.14 keeps `...` on the same line as the closing `}`. That is the only
difference found -- every per-subcommand `--help` (options, choices,
defaults, help strings) and both completion scripts (zsh/bash) are
byte-identical between 3.11 and 3.14. Rather than normalize the wrapping
difference away (which would hide a real formatting regression in a
refactor), only the root-parser snapshot is versioned: 3.11 uses
tests/snapshots/help/py311/_root.txt, other versions use
tests/snapshots/help/_root.txt. All per-subcommand snapshots and both
completion snapshots are shared across versions.

Separately: on 3.13+, argparse derives a parser's default `prog` from
`sys.modules["__main__"].__spec__` when the running module was launched via
`-m` (as both `python -m unittest` and `python -m pytest` are), ignoring
`sys.argv[0]` -- see `_build_parser()` below for how that is neutralized so
`prog` is "xswap" regardless of Python version or runner.

Regeneration
------------
Run with XSWAP_UPDATE_SNAPSHOTS=1 to rewrite every snapshot file from the
current output instead of asserting against it, e.g.:

    XSWAP_UPDATE_SNAPSHOTS=1 python -m unittest test_help_snapshots
"""

from __future__ import annotations

import difflib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_swap
import xswap_completion

SNAPSHOT_ROOT = Path(__file__).parent / "tests" / "snapshots"
HELP_DIR = SNAPSHOT_ROOT / "help"
HELP_DIR_PY311 = HELP_DIR / "py311"
COMPLETION_DIR = SNAPSHOT_ROOT / "completion"

UPDATE_ENV_VAR = "XSWAP_UPDATE_SNAPSHOTS"

# Derived from codex_swap.parser() at test time (see test_subcommand_set_matches_expected)
# and pinned here so a silently dropped or added subcommand fails loudly.
EXPECTED_SUBCOMMANDS = {
    "init", "register", "add", "login", "list", "usage", "menubar", "dashboard",
    "status", "auto-status", "auto-tick", "auto-enable", "auto-policy",
    "repair-plugins", "relocate-codex", "doctor", "auto-disable", "upgrade",
    "alert", "use", "switch", "disable", "enable", "remove", "map", "unmap",
    "openclaw", "run", "app", "completion",
}

PRIVATE_MARKERS = ("bigno", "intellieffect", "@", "/Users/", "/Volumes/")


def _is_py311():
    return sys.version_info[:2] == (3, 11)


def _root_help_path():
    return (HELP_DIR_PY311 if _is_py311() else HELP_DIR) / "_root.txt"


def _subcommand_help_path(name):
    return HELP_DIR / f"{name}.txt"


def _canonical_subcommand_names(p):
    """Every distinct subparser exactly once, keyed by its canonical (first) name."""
    sub_action = p._subparsers._group_actions[0]
    seen_ids = {}
    names = []
    for name, subparser in sub_action.choices.items():
        key = id(subparser)
        if key not in seen_ids:
            seen_ids[key] = name
            names.append(name)
    return names


def _build_parser():
    """Build a fresh parser with a stable prog ("xswap").

    `codex_swap.parser()` builds its `ArgumentParser` without an explicit
    `prog=`, so argparse computes a default `prog` at *construction* time
    (not lazily read at `format_help()` time), so both patches below must be
    active around the `parser()` call itself, not just around `format_help`.

    Two argparse behaviors have to be neutralized:
    - Pre-3.13: the default is `os.path.basename(sys.argv[0])`, so `sys.argv`
      must not be left as whatever launched the test (e.g. a pytest binary).
    - 3.13+ (confirmed different on 3.14.6 vs 3.11.14 here): when the running
      module was invoked via `-m` (as `python -m unittest` / `python -m
      pytest` both are), argparse's `_prog_name` ignores `sys.argv[0]`
      entirely and instead derives `"{python} -m {module}"` from
      `sys.modules["__main__"].__spec__`. So `__main__.__spec__` must also be
      cleared, which makes `_prog_name` fall back to the `sys.argv[0]` path
      on every Python version tested.
    """
    with patch.object(sys, "argv", ["xswap"]), patch.object(
        sys.modules["__main__"], "__spec__", None
    ):
        return codex_swap.parser()


def _format_help(p):
    # 3.13+ argparse colorizes help when stdout is a TTY or FORCE_COLOR is set.
    # PYTHON_COLORS=0 outranks both, so a run from a terminal (and a snapshot
    # regeneration from one) yields the same bytes as a captured CI run.
    with patch.dict(os.environ, {"COLUMNS": "80", "PYTHON_COLORS": "0", "NO_COLOR": "1"}):
        return p.format_help()


def _assert_snapshot(test, path, actual):
    if os.environ.get(UPDATE_ENV_VAR):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    if not path.exists():
        test.fail(
            f"Missing snapshot {path}. Regenerate with "
            f"`{UPDATE_ENV_VAR}=1 python -m unittest test_help_snapshots`."
        )
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(path),
                tofile="actual",
            )
        )
        test.fail(
            f"Snapshot mismatch for {path}.\n"
            f"If this change is intentional, regenerate with "
            f"`{UPDATE_ENV_VAR}=1 python -m unittest test_help_snapshots`.\n\n"
            f"{diff}"
        )


class HelpSnapshotTests(unittest.TestCase):
    def test_subcommand_set_matches_expected(self):
        names = set(_canonical_subcommand_names(_build_parser()))
        # "switch" is an alias of "use" and is not a canonical subparser name
        # (argparse groups it under "use"), so it is not expected here even
        # though it is a real, user-facing subcommand -- see the completion
        # test for alias coverage.
        expected_canonical = EXPECTED_SUBCOMMANDS - {"switch"}
        self.assertEqual(names, expected_canonical)
        self.assertEqual(len(names), len(EXPECTED_SUBCOMMANDS) - 1)

    def test_root_help(self):
        actual = _format_help(_build_parser())
        _assert_snapshot(self, _root_help_path(), actual)

    def test_subcommand_help_snapshots(self):
        p = _build_parser()
        sub_action = p._subparsers._group_actions[0]
        for name in _canonical_subcommand_names(p):
            subparser = sub_action.choices[name]
            with self.subTest(subcommand=name):
                actual = _format_help(subparser)
                _assert_snapshot(self, _subcommand_help_path(name), actual)


class CompletionSnapshotTests(unittest.TestCase):
    def _generate_isolated(self, shell):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CODEX_SWAP_HOME": tmp}):
                return xswap_completion.generate(_build_parser(), shell)

    def test_zsh_completion_snapshot(self):
        actual = self._generate_isolated("zsh")
        _assert_snapshot(self, COMPLETION_DIR / "zsh.txt", actual)

    def test_bash_completion_snapshot(self):
        actual = self._generate_isolated("bash")
        _assert_snapshot(self, COMPLETION_DIR / "bash.txt", actual)

    def test_snapshots_contain_no_private_data(self):
        for path in sorted(SNAPSHOT_ROOT.rglob("*.txt")):
            text = path.read_text(encoding="utf-8").lower()
            for marker in PRIVATE_MARKERS:
                self.assertNotIn(
                    marker.lower(), text, f"{path} leaks private marker {marker!r}"
                )


if __name__ == "__main__":
    unittest.main()
