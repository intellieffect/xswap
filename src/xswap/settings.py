"""Compatibility alias: `xswap.settings` is now `xswap.core.settings` (it is platform-neutral).

This is not a re-export. The module object below *is* `xswap.core.settings`, installed under
the old name, so the two are the same object: whatever the suite or an embedder
imports, patches or monkey-patches through either path reaches the other. That
is what keeps `patch("xswap.settings.<anything>")` biting after the move. Kept
for one release (INT-5614).
"""
import sys

from xswap.core import settings as _module

sys.modules[__name__] = _module
