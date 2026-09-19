"""Compatibility alias: `xswap.launcher` is now `xswap.providers.codex.launcher` (it is Codex-specific).

This is not a re-export. The module object below *is* `xswap.providers.codex.launcher`, installed under
the old name, so the two are the same object: whatever the suite or an embedder
imports, patches or monkey-patches through either path reaches the other. That
is what keeps `patch("xswap.launcher.<anything>")` biting after the move. Kept
for one release (INT-5614).
"""
import sys

from xswap.providers.codex import launcher as _module

sys.modules[__name__] = _module
