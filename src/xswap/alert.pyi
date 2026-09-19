# Type stub for the compatibility alias `xswap.alert` (INT-5612).
# The .py file replaces this module object with `xswap.core.alert` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.alert import X` as `from xswap.core.alert import X`.
from xswap.core.alert import *
