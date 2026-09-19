# Type stub for the compatibility alias `xswap.launcher` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.launcher` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.launcher import X` as `from xswap.providers.codex.launcher import X`.
from xswap.providers.codex.launcher import *
