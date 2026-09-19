"""Every public function/method under `xswap` has full parameter and return annotations.

Walks the real package tree (not the flat compatibility shims -- each one is just
`sys.modules[__name__] = <target module>`, so it carries the target's own annotations
under its own name, and re-checking it here would just check the target twice) plus
`xswap.bridge`, which is a separate optional extra outside INT-5612's surface.

"Public" means a name that does not start with `_` (module-level function or class,
or a method on such a class). `self`/`cls` and bare `*args`/`**kwargs` are exempt --
annotating them is either impossible (`self`) or not what callers care about; a named
`*args: int` or `**kwargs: str` is still required to be annotated like any other
parameter.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import unittest
from types import FunctionType

import xswap

SKIP_MODULES = {"xswap.bridge"}

# The flat top-level alias shims (INT-5612/INT-5614): each is `sys.modules[__name__] =
# <moved module>`, so `import xswap.<name>` returns the *target* module object, not a
# distinct module of its own -- walking the package tree below never reaches these as
# separate modules, but list them so the intent is explicit if that ever changes.
ALIAS_SHIMS = {
    "alert", "auth_state", "codex_cli", "completion", "credentials", "display",
    "doctor", "errors", "exit_codes", "fsutil", "identity", "init", "json_output",
    "launcher", "live", "locking", "mappings", "menubar", "openclaw_state",
    "openclaw_sync", "path", "paths", "plugins", "ranking", "registry", "relocate",
    "reports", "runs", "settings", "switch", "tick", "upgrade", "usage",
    "usage_cache", "wrapper",
}


def iter_modules():
    """Every real `xswap.*` submodule, skipping the flat shims and `xswap.bridge`."""
    prefix = xswap.__name__ + "."
    for info in pkgutil.walk_packages(xswap.__path__, prefix):
        name = info.name
        if name in SKIP_MODULES or any(name.startswith(skip + ".") for skip in SKIP_MODULES):
            continue
        top = name[len(prefix):].split(".", 1)[0]
        if top in ALIAS_SHIMS:
            continue
        yield name


def public_functions(module):
    """(qualified name, function) for every public top-level function and method
    defined IN this module (not merely imported into it)."""
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if isinstance(obj, FunctionType) and obj.__module__ == module.__name__:
            yield f"{module.__name__}.{name}", obj
        elif inspect.isclass(obj) and obj.__module__ == module.__name__:
            for meth_name, meth in vars(obj).items():
                if meth_name.startswith("_"):
                    continue
                func = meth.__func__ if isinstance(meth, (staticmethod, classmethod)) else meth
                if isinstance(func, FunctionType) and func.__module__ == module.__name__:
                    yield f"{module.__name__}.{obj.__name__}.{meth_name}", func


def missing_annotations(qualname, func):
    """Parameter/return names on FUNC that have no annotation; `self`/`cls` and bare
    `*args`/`**kwargs` (unnamed-style, i.e. without their own type) are exempt."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return []
    missing = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.annotation is inspect.Parameter.empty:
            missing.append(f"{qualname}({pname})")
    if sig.return_annotation is inspect.Signature.empty:
        missing.append(f"{qualname} -> return")
    return missing


class PublicSurfaceIsAnnotated(unittest.TestCase):
    def test_every_public_function_and_method_is_annotated(self):
        offenders = []
        checked = 0
        for module_name in iter_modules():
            module = importlib.import_module(module_name)
            for qualname, func in public_functions(module):
                checked += 1
                offenders.extend(missing_annotations(qualname, func))
        self.assertGreater(checked, 0, "no public functions/methods were found to check")
        if offenders:
            self.fail(
                f"{len(offenders)} unannotated parameter(s)/return(s) out of "
                f"{checked} public callables:\n" + "\n".join(sorted(offenders))
            )


if __name__ == "__main__":
    unittest.main()
