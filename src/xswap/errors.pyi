# Type stub for the compatibility alias `xswap.errors` (INT-5612).
# The .py file replaces this module object with `xswap.core.errors` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.errors import X` as `from xswap.core.errors import X`.
from xswap.core.errors import *
