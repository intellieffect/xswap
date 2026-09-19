# Type stub for the compatibility alias `xswap.init` (INT-5612).
# The .py file replaces this module object with `xswap.providers.codex.init` at runtime
# (sys.modules[__name__] = ...); pyright cannot follow that dynamic swap, so this
# stub tells it to type-check `from xswap.init import X` as `from xswap.providers.codex.init import X`.
from xswap.providers.codex.init import *
