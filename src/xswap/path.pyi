# Type stub for the compatibility alias `xswap.path` (INT-5612).
# The .py file replaces this module object with `xswap.core.path` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.path import X` as `from xswap.core.path import X`.
from xswap.core.path import *
