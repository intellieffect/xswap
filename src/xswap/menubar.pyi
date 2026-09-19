# Type stub for the compatibility alias `xswap.menubar` (INT-5612).
# The .py file replaces this module object with `xswap.core.menubar` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.menubar import X` as `from xswap.core.menubar import X`.
from xswap.core.menubar import *
