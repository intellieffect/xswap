# Type stub for the compatibility alias `xswap.switch` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.switch` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.switch import X` as `from xswap.providers.codex.switch import X`.
from xswap.providers.codex.switch import *
