# Type stub for the compatibility alias `xswap.live` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.live` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.live import X` as `from xswap.providers.codex.live import X`.
from xswap.providers.codex.live import *
