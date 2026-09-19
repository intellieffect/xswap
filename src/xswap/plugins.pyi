# Type stub for the compatibility alias `xswap.plugins` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.plugins` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.plugins import X` as `from xswap.providers.codex.plugins import X`.
from xswap.providers.codex.plugins import *
