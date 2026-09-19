# Type stub for the compatibility alias `xswap.fsutil` (INT-5612).
# The .py file replaces this module object with `xswap.core.fsutil` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.fsutil import X` as `from xswap.core.fsutil import X`.
from xswap.core.fsutil import *
