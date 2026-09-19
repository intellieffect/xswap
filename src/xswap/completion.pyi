# Type stub for the compatibility alias `xswap.completion` (INT-5612).
# The .py file replaces this module object with `xswap.core.completion` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.completion import X` as `from xswap.core.completion import X`.
from xswap.core.completion import *
