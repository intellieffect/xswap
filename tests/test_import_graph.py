"""The leaf modules must stay leaves.

`errors`, `fsutil`, `paths` and `locking` exist so that any module can use them
without an import cycle. That only holds while importing one of them pulls in
nothing else from `xswap` -- which a top-level import added later, or an eager
import in `xswap/__init__.py`, would silently undo. Each case starts a fresh
interpreter so an already-imported sibling cannot hide the regression.
"""
import pathlib
import re
import subprocess
import sys
import unittest

# `xswap` itself is unavoidable (importing a submodule runs the package), and
# `xswap._version` is what its __init__ reads for `xswap.__version__`. Since
# INT-5614 the neutral modules live in `xswap.core`, so its package body runs too.
PACKAGE = {"xswap", "xswap._version", "xswap.core"}

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "src" / "xswap"


def imported_by(module):
    code = (f"import importlib, sys; importlib.import_module({module!r}); "
            "print(' '.join(sorted(m for m in sys.modules if m.startswith('xswap'))))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return set(result.stdout.split())


class LeafModules(unittest.TestCase):
    def test_errors_imports_nothing_else_from_xswap(self):
        self.assertEqual(imported_by("xswap.core.errors"), PACKAGE | {"xswap.core.errors"})

    def test_fsutil_imports_nothing_else_from_xswap(self):
        self.assertEqual(imported_by("xswap.core.fsutil"), PACKAGE | {"xswap.core.fsutil"})

    def test_paths_imports_nothing_else_from_xswap(self):
        self.assertEqual(imported_by("xswap.core.paths"), PACKAGE | {"xswap.core.paths"})

    def test_locking_imports_only_errors_from_xswap(self):
        self.assertLessEqual(imported_by("xswap.core.locking"), PACKAGE | {"xswap.core.locking", "xswap.core.errors"})


class CollaboratorModules(unittest.TestCase):
    """`Manager`'s collaborators must not import their own facade.

    `manager` imports every one of them at module level, so a top-level
    `import xswap.manager` back would be a cycle -- and a lazy one inside a
    function would quietly re-create the god object this split removed.
    `codex_cli` is listed with it because it imports `manager`, so reaching it
    at import time is the same cycle one step out. Both are still allowed
    *inside* a function body, which is how `registry` and `launcher` reach
    `read_settings`; only the import-time graph is checked here.
    """

    MODULES = ("xswap.core.ranking", "xswap.providers.codex.identity", "xswap.core.reports",
               "xswap.core.registry", "xswap.core.mappings", "xswap.core.usage_cache",
               "xswap.core.auth_state", "xswap.providers.codex.openclaw_sync",
               "xswap.providers.codex.launcher")

    def test_collaborators_do_not_import_the_facade(self):
        for module in self.MODULES:
            with self.subTest(module=module):
                imported = imported_by(module)
                self.assertNotIn("xswap.manager", imported)
                self.assertNotIn("xswap.providers.codex.codex_cli", imported)


class CodexCliLayers(unittest.TestCase):
    """The layers `codex_cli` was split into must stay layers (INT-5610).

    `settings` reads auto.json, `wrapper` owns the codex symlink, `runs` owns the
    bridge run records, and only `codex_cli` -- the live bridge and the launcher --
    sits on top of all three. Reaching back up at import time would restore the
    1,600-line module the split removed, and would put the whole bridge (asyncio,
    the WebSocket protocol) behind `doctor` and `tick` again. Function-local
    imports are still allowed: that is how `wrapper._late` and `runs.reopen_command`
    keep the suite's `patch('xswap.codex_cli.<name>')` targets biting.
    """

    LAYERS = ("xswap.core.settings", "xswap.providers.codex.wrapper", "xswap.providers.codex.runs")

    def test_layers_do_not_import_the_bridge_or_the_command_line(self):
        for module in self.LAYERS:
            with self.subTest(module=module):
                imported = imported_by(module)
                self.assertNotIn("xswap.providers.codex.codex_cli", imported)
                self.assertNotIn("xswap.cli", imported)

    def test_settings_is_below_the_wrapper_and_the_run_records(self):
        imported = imported_by("xswap.core.settings")
        self.assertNotIn("xswap.providers.codex.wrapper", imported)
        self.assertNotIn("xswap.providers.codex.runs", imported)

    def test_doctor_and_tick_no_longer_pull_in_the_bridge(self):
        # Both used to import `codex_cli` at module level for `read_settings` and the
        # wrapper/run helpers; they now take them from `settings`, `wrapper` and `runs`.
        for module in ("xswap.providers.codex.doctor", "xswap.core.tick"):
            with self.subTest(module=module):
                self.assertNotIn("xswap.providers.codex.codex_cli", imported_by(module))


class ProviderBoundary(unittest.TestCase):
    """`core` must stay below `providers` (INT-5614).

    The whole point of the split is that a second platform can be added without
    editing `xswap.core`. Two things have to hold for that, and both erode
    silently: core must not reach *up* to a provider, the facade or the command
    line at import time, and core must not contain any one platform's
    vocabulary -- the moment a Codex name appears in a neutral module, the next
    platform has to edit it.
    """

    # Every module under src/xswap/core, as a dotted name.
    CORE_MODULES = tuple(sorted(
        "xswap.core." + path.stem for path in (SOURCE / "core").glob("*.py") if path.stem != "__init__"))

    # The only place in `xswap.core` that may name a platform, and why. Both are
    # identifiers already on users' disks and in users' shell profiles: renaming
    # either would move every installed state root and orphan every account.
    ALLOWED = {"xswap/core/paths.py": ("CODEX_SWAP_HOME", "codex-swap")}

    VOCABULARY = re.compile(r"codex|chatgpt|openai|auth\.json", re.I)

    def test_core_modules_import_no_provider_facade_or_cli(self):
        for module in self.CORE_MODULES:
            with self.subTest(module=module):
                imported = imported_by(module)
                self.assertEqual([m for m in imported if m.startswith("xswap.providers")], [])
                self.assertNotIn("xswap.manager", imported)
                self.assertNotIn("xswap.cli", imported)

    def test_the_protocol_and_the_neutral_types_import_no_provider(self):
        for module in ("xswap.providers.base", "xswap.core.types"):
            with self.subTest(module=module):
                self.assertEqual(
                    [m for m in imported_by(module) if m.startswith("xswap.providers.codex")], [])

    def test_core_never_names_a_platform_outside_the_allow_list(self):
        offenders = []
        for path in sorted((SOURCE / "core").glob("*.py")):
            key = "xswap/core/" + path.name
            allowed = self.ALLOWED.get(key, ())
            for number, line in enumerate(path.read_text().splitlines(), 1):
                for match in self.VOCABULARY.finditer(line):
                    if any(token.lower() in line.lower() for token in allowed):
                        continue
                    offenders.append(f"{key}:{number}: {match.group(0)!r} in {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_every_moved_module_is_still_importable_under_its_old_name(self):
        """The old flat names are aliases of the moved modules, not copies.

        A copy would make `patch("xswap.usage.read_limits")` patch a name nothing
        reads. Same module object, or the compatibility promise is empty.
        """
        code = ("import importlib;"
                "old = importlib.import_module('xswap.{old}');"
                "new = importlib.import_module('{new}');"
                "print(old is new)")
        pairs = [("errors", "xswap.core.errors"), ("tick", "xswap.core.tick"),
                 ("display", "xswap.core.display"), ("usage_cache", "xswap.core.usage_cache"),
                 ("usage", "xswap.providers.codex.usage"), ("live", "xswap.providers.codex.live"),
                 ("codex_cli", "xswap.providers.codex.codex_cli"),
                 ("wrapper", "xswap.providers.codex.wrapper"),
                 ("doctor", "xswap.providers.codex.doctor")]
        for old, new in pairs:
            with self.subTest(module=old):
                result = subprocess.run([sys.executable, "-c", code.format(old=old, new=new)],
                                        capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
