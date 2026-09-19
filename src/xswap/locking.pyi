# Type stub for the compatibility alias `xswap.locking` (INT-5612).
# The .py file replaces this module object with `xswap.core.locking` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.locking import X` as `from xswap.core.locking import X`.
from xswap.core.locking import *
