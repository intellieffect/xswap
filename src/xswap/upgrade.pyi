# Type stub for the compatibility alias `xswap.upgrade` (INT-5612).
# The .py file replaces this module object with `xswap.core.upgrade` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.upgrade import X` as `from xswap.core.upgrade import X`.
from xswap.core.upgrade import *
