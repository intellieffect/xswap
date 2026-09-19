"""Compatibility alias: `xswap.alert` is now `xswap.core.alert` (it is platform-neutral).

This is not a re-export. The module object below *is* `xswap.core.alert`, installed under
the old name, so the two are the same object: whatever the suite or an embedder
imports, patches or monkey-patches through either path reaches the other. That
is what keeps `patch("xswap.alert.<anything>")` biting after the move. Kept
for one release (INT-5614).
"""
import sys

from xswap.core import alert as _module

sys.modules[__name__] = _module
