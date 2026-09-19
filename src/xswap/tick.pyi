# Type stub for the compatibility alias `xswap.tick` (INT-5612).
# The .py file replaces this module object with `xswap.core.tick` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.tick import X` as `from xswap.core.tick import X`.
from xswap.core.tick import *
