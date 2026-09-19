# Type stub for the compatibility alias `xswap.settings` (INT-5612).
# The .py file replaces this module object with `xswap.core.settings` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.settings import X` as `from xswap.core.settings import X`.
from xswap.core.settings import *
