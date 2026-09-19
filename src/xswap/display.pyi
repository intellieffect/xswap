# Type stub for the compatibility alias `xswap.display` (INT-5612).
# The .py file replaces this module object with `xswap.core.display` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.display import X` as `from xswap.core.display import X`.
from xswap.core.display import *
