"""Compatibility alias: `xswap.path` is now `xswap.core.path` (it is platform-neutral).

This is not a re-export. The module object below *is* `xswap.core.path`, installed under
the old name, so the two are the same object: whatever the suite or an embedder
imports, patches or monkey-patches through either path reaches the other. That
is what keeps `patch("xswap.path.<anything>")` biting after the move. Kept
for one release (INT-5614).
"""
import sys

from xswap.core import path as _module

sys.modules[__name__] = _module
