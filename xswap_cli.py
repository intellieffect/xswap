"""Compatibility shim: `xswap_cli` moved to `xswap.codex_cli` (INT-5607).

Kept for one release so an already-running older bridge process, or a
script that still does `import xswap_cli`, keeps working. Remove after the
next release.
"""

from xswap.codex_cli import *  # noqa: F401,F403
from xswap.codex_cli import codex_main  # noqa: F401
