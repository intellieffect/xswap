"""Compatibility shim: `codex_swap` moved to `xswap.manager` (INT-5607).

Kept for one release so an already-running older bridge process, or a
script that still does `import codex_swap`, keeps working. Remove after the
next release.
"""

from xswap.manager import *  # noqa: F403
from xswap.manager import (  # noqa: F401
    Manager,
    __version__,
    main,
    parser,
)
