# Type stub for the compatibility alias `xswap.paths` (INT-5612).
# The .py file replaces this module object with `xswap.core.paths` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.paths import X` as `from xswap.core.paths import X`.
from xswap.core.paths import *
