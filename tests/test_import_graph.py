"""The leaf modules must stay leaves.

`errors`, `fsutil`, `paths` and `locking` exist so that any module can use them
without an import cycle. That only holds while importing one of them pulls in
nothing else from `xswap` -- which a top-level import added later, or an eager
import in `xswap/__init__.py`, would silently undo. Each case starts a fresh
interpreter so an already-imported sibling cannot hide the regression.
"""
import subprocess
import sys
import unittest

# `xswap` itself is unavoidable (importing a submodule runs the package), and
# `xswap._version` is what its __init__ reads for `xswap.__version__`.
PACKAGE = {"xswap", "xswap._version"}


def imported_by(module):
    code = (f"import importlib, sys; importlib.import_module({module!r}); "
            "print(' '.join(sorted(m for m in sys.modules if m.startswith('xswap'))))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return set(result.stdout.split())


class LeafModules(unittest.TestCase):
    def test_errors_imports_nothing_else_from_xswap(self):
        self.assertEqual(imported_by("xswap.errors"), PACKAGE | {"xswap.errors"})

    def test_fsutil_imports_nothing_else_from_xswap(self):
        self.assertEqual(imported_by("xswap.fsutil"), PACKAGE | {"xswap.fsutil"})

    def test_paths_imports_nothing_else_from_xswap(self):
        self.assertEqual(imported_by("xswap.paths"), PACKAGE | {"xswap.paths"})

    def test_locking_imports_only_errors_from_xswap(self):
        self.assertLessEqual(imported_by("xswap.locking"), PACKAGE | {"xswap.locking", "xswap.errors"})


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

    MODULES = ("xswap.ranking", "xswap.identity", "xswap.reports", "xswap.registry",
               "xswap.mappings", "xswap.usage_cache", "xswap.auth_state",
               "xswap.openclaw_sync", "xswap.launcher")

    def test_collaborators_do_not_import_the_facade(self):
        for module in self.MODULES:
            with self.subTest(module=module):
                imported = imported_by(module)
                self.assertNotIn("xswap.manager", imported)
                self.assertNotIn("xswap.codex_cli", imported)


if __name__ == "__main__":
    unittest.main()
