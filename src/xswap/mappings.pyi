# Type stub for the compatibility alias `xswap.mappings` (INT-5612).
# The .py file replaces this module object with `xswap.core.mappings` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.mappings import X` as `from xswap.core.mappings import X`.
from xswap.core.mappings import *
