# Type stub for the compatibility alias `xswap.registry` (INT-5612).
# The .py file replaces this module object with `xswap.core.registry` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.registry import X` as `from xswap.core.registry import X`.
from xswap.core.registry import *
